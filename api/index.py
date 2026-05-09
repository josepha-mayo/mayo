
import os
import json
import re
import requests
import hmac
import hashlib
import time
from flask import Flask, request, jsonify
from github import Github, GithubIntegration
from github.Auth import AppAuth, Token

app = Flask(__name__)

EXCLUDED_REPOS = ['Square-farms', 'Jo-ayanda-real-estate', 'Backend-images-app', 'ecom-stor']

def co_author_msg(msg):
    co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
    co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
    if co_author_name and co_author_email:
        return f"{msg}\n\nCo-authored-by: {co_author_name} <{co_author_email}>"
    return msg

# Config
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI2_API_KEY = os.environ.get('GEMINI2_API_KEY')
GROK_API_KEY = os.environ.get('GROK_API_KEY')
GEMINI_FALLBACK_API_KEY = os.environ.get('GEMINI_FALLBACK_API_KEY')
GEMINI2_FALLBACK_API_KEY = os.environ.get('GEMINI2_FALLBACK_API_KEY')
GROK_FALLBACK_API_KEY = os.environ.get('GROK_FALLBACK_API_KEY')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
FIREWORKS_API_KEY = os.environ.get('FIREWORKS_API_KEY')
FIREWORKS2_API_KEY = os.environ.get('FIREWORKS2_API_KEY')
NVIDIA_API_KEY = os.environ.get('NVIDIA_API_KEY')
GEMINI_NEWCRONS_API_KEY = os.environ.get('GEMINI_NEWCRONS_API_KEY')
GROQ_NEWCRONS_API_KEY = os.environ.get('GROQ_NEWCRONS_API_KEY')
APP_ID = os.environ.get('APP_ID')
PRIVATE_KEY = os.environ.get('PRIVATE_KEY', '').replace('\\n', '\n')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET')
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Helper: Verify Webhook Signature
def verify_signature(req):
    signature = req.headers.get('X-Hub-Signature-256')
    if not WEBHOOK_SECRET:
        print("ERROR: WEBHOOK_SECRET is not set in environment variables.")
        return False, "WEBHOOK_SECRET_MISSING"
    if not signature:
        print("ERROR: No X-Hub-Signature-256 header provided.")
        return False, "SIGNATURE_MISSING"
    
    try:
        sha_name, signature_hash = signature.split('=')
    except ValueError:
        return False, "INVALID_SIGNATURE_FORMAT"

    if sha_name != 'sha256':
        return False, "UNSUPPORTED_HASH_ALGORITHM"
    
    mac = hmac.new(WEBHOOK_SECRET.encode('utf-8'), req.data, hashlib.sha256)
    expected = mac.hexdigest()
    if not hmac.compare_digest(expected.encode('utf-8'), signature_hash.encode('utf-8')):
        safe_expected = expected[:8]
        safe_got = signature_hash[:8]
        print(f"ERROR: Signature mismatch. Expected {safe_expected}... but got {safe_got}... (Payload len: {len(req.data)})")
        return False, f"SIGNATURE_MISMATCH (len:{len(req.data)})"
    
    return True, "OK"

# Helper: Get GitHub Client for Installation
def get_installation_token(installation_id):
    integration = GithubIntegration(auth=AppAuth(APP_ID, PRIVATE_KEY))
    return integration.get_access_token(installation_id).token

def get_github_client(installation_id):
    token = get_installation_token(installation_id)
    return Github(auth=Token(token))

# Helper: Get Bot Login
BOT_LOGIN_CACHE = None
def get_bot_login():
    global BOT_LOGIN_CACHE
    if BOT_LOGIN_CACHE:
        return BOT_LOGIN_CACHE
    try:
        integration = GithubIntegration(auth=AppAuth(APP_ID, PRIVATE_KEY))
        BOT_LOGIN_CACHE = f"{integration.get_app().slug}[bot]"
        return BOT_LOGIN_CACHE
    except Exception as e:
        print(f"Error getting bot login: {e}")
        return "joe-gemini-bot[bot]"



def fetch_memory(repo, issue_number, bot_login):
    """Read bot's previous comments and extract [MEMORY] blocks."""
    try:
        issue = repo.get_issue(number=issue_number)
        files_read_accum: list = []
        context_summary_acc: str = ""
        
        for comment in issue.get_comments():
            if comment.user.login.lower() == bot_login.lower():
                body = comment.body
                # Look for hidden memory block
                memory_match = re.search(r'<!-- \[MEMORY\]([\s\S]*?)\[/MEMORY\] -->', body)
                if memory_match:
                    try:
                        mem = json.loads(memory_match.group(1).strip())
                        mem_files = mem.get('files_read')
                        if isinstance(mem_files, list):
                            files_read_accum = files_read_accum + mem_files
                        
                        mem_summary = mem.get('context_summary')
                        if isinstance(mem_summary, str):
                            context_summary_acc = mem_summary
                    except json.JSONDecodeError:
                        pass
        
        # Deduplicate files
        return {"files_read": list(set(files_read_accum)), "context_summary": context_summary_acc}
    except Exception as e:
        print(f"Memory fetch error: {e}")
        return {"files_read": [], "context_summary": ""}

def format_memory_block(data):
    """Format memory data as a hidden HTML comment."""
    return f"\n\n<!-- [MEMORY]{json.dumps(data)}[/MEMORY] -->"

def get_repo_structure(repo, path="", max_depth=1, current_depth=0):
    """Get repository file structure via GitHub API (single level to avoid timeout)."""
    if int(current_depth) > int(max_depth):
        return ""
    
    structure = ""
    try:
        contents = repo.get_contents(path)
        # Sort: dirs first, then files
        items = sorted(contents, key=lambda x: (x.type != 'dir', x.name))
        
        for i, item in enumerate(items):
            if i >= 30: break
            if item.name.startswith('.'):
                continue
            
            indent = "".join(["  " for _ in range(int(current_depth))])
            marker = "📁 " if item.type == 'dir' else "📄 "
            structure += f"{indent}{marker}{item.name}\n"
            
            # Only go 1 level deep to avoid timeout
            if item.type == 'dir' and int(current_depth) < int(max_depth):
                structure += get_repo_structure(repo, item.path, max_depth, int(current_depth) + 1)
    except Exception as e:
        print(f"Repo structure error: {e}")
        structure = f"Error: {e}\n"
    
    return structure

def parse_diff_files(diff_text):
    """Parse unified diff to extract changed files with their line ranges."""
    files = []
    current_file = None
    current_lines = []
    
    for line in diff_text.split('\n'):
        # New file in diff
        if line.startswith('+++ b/'):
            if current_file:
                files.append({'path': current_file, 'lines': current_lines})
            current_file = line[6:]  # Remove '+++ b/'
            current_lines = []
        # Hunk header: @@ -old,count +new,count @@
        elif line.startswith('@@') and current_file:
            import re as _re
            match = _re.search(r'\+(\d+)(?:,(\d+))?', line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                current_lines.append({'start': start, 'end': start + count - 1})
    
    if current_file:
        files.append({'path': current_file, 'lines': current_lines})
    
    return files

def read_file_content(repo, file_path):
    """Read file content from repo."""
    EXCLUDED_FILES = ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lockb', '.min.js', '.min.css']
    if any(excl in file_path for excl in EXCLUDED_FILES):
        print(f"Skipping reading {file_path} (excluded file type)")
        return None

    try:
        content = repo.get_contents(file_path)
        return content.decoded_content.decode('utf-8')
    except Exception as e:
        print(f"File read error for {file_path}: {e}")
        return None

def get_context_expansion_files(prompt, initial_context):
    """Ask Gemini what files it needs to read."""
    analysis_prompt = f"""You are an expert developer.

User Request: {prompt}

Current Context:
{initial_context}

Task: Determine if you need to read any specific files from the repository to answer accurately or verify syntax/conventions.
If you need files, list them as a JSON array. If you have enough info, return [].

Response Format:
```json
["path/to/file1.ext", "path/to/file2.ext"]
```
Do not explain. Just return the JSON.
"""
    response, _ = query_gemini(analysis_prompt, initial_context)
    return extract_json_from_response(response)


def extract_json_from_response(text):
    if not text: return None
    json_patterns = [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```', r'\{[\s\S]*"edits"[\s\S]*\}']
    
    for pattern in json_patterns:
        match = re.search(pattern, text)
        if match:
            json_str = match.group(1) if '```' in pattern else match.group(0)
            try:
                return json.loads(json_str)
            except Exception as e:
                # Attempt to fix the string before failing completely
                try:
                    import ast
                    parsed = ast.literal_eval(json_str.strip())
                    if isinstance(parsed, dict):
                         return parsed
                except Exception:
                    pass
                
                # Fix unescaped newlines in strings (common LLM hallucination)
                try:
                    def escape_newlines_in_json_strings(s):
                        result = []
                        in_string = False
                        escaped = False
                        for char in s:
                            if char == '"' and not escaped:
                                in_string = not in_string
                            elif char == '\\' and not escaped:
                                escaped = True
                            else:
                                escaped = False
                                
                            if in_string and char == '\n':
                                result.append('\\n')
                            elif in_string and char == '\r':
                                result.append('\\r')
                            elif in_string and char == '\t':
                                result.append('\\t')
                            else:
                                result.append(char)
                        return "".join(result)
                    
                    fixed_str = escape_newlines_in_json_strings(json_str)
                    return json.loads(fixed_str)
                except Exception:
                    pass
                continue
    return None

def commit_changes_via_api(repo, branch_name, file_changes, commit_message):
    try:
        try:
            repo.get_branch(branch_name)
        except Exception:
            # Branch doesn't exist, create it from default branch
            sb = repo.get_branch(repo.default_branch)
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=sb.commit.sha)
        
        # Add co-author if configured
        co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
        co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
        if co_author_name and co_author_email:
            commit_message = f"{commit_message}\n\nCo-authored-by: {co_author_name} <{co_author_email}>"
        
        for path, content in file_changes.items():
            try:
                contents = repo.get_contents(path, ref=branch_name)
                repo.update_file(path, commit_message, content, contents.sha, branch=branch_name)
            except:
                repo.create_file(path, commit_message, content, branch=branch_name)
        return True, ""
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        print(f"API Commit Error: {e}")
        return False, str(e) + "\n" + err_msg

def apply_surgical_edits(content, edits):
    """Apply search/replace blocks using LINE-BASED matching (not substring).
    
    This prevents partial-line matches from cascading into mass deletions.
    The search block is split into lines, matched against complete file lines,
    and only the matched line range is replaced.
    """
    content_lines_raw = content.splitlines(True)  # Keep line endings
    c_map = {}
    if isinstance(content_lines_raw, list):
        for line_idx, line in enumerate(content_lines_raw):
            if isinstance(line, str):
                c_map[int(line_idx)] = line
    
    original_line_count = len(c_map)
    
    for edit in edits:
        search = edit.get('search')
        replace = edit.get('replace')
        if not search:
            continue
        
        search_lines_raw = search.splitlines()
        s_map = {}
        if isinstance(search_lines_raw, list):
            for sl_idx, sl in enumerate(search_lines_raw):
                if isinstance(sl, str):
                    s_map[int(sl_idx)] = sl
        search_lines_count = len(s_map)
        replace_text = replace if replace else ""
        
        # Find the search block in content using LINE-BY-LINE matching
        # Pass 1: Strict match (rstrip only)
        match_start = -1
        for enum_i in range(original_line_count - search_lines_count + 1):
            matched = True
            for j in range(search_lines_count):
                search_line = str(s_map.get(int(j), ""))
                content_line = str(c_map.get(int(enum_i) + int(j), "")).rstrip('\\r\\n')
                if content_line.rstrip() != search_line.rstrip():
                    matched = False
                    break
            if matched:
                match_start = enum_i
                break
        
        # Pass 2: Fuzzy match (strip ALL whitespace) — handles indentation mismatches from LLMs
        if match_start == -1:
            for enum_i in range(original_line_count - search_lines_count + 1):
                matched = True
                for j in range(search_lines_count):
                    search_line = str(s_map.get(int(j), ""))
                    content_line = str(c_map.get(int(enum_i) + int(j), "")).rstrip('\\r\\n')
                    if content_line.strip() != search_line.strip():
                        matched = False
                        break
                if matched:
                    match_start = enum_i
                    print(f"DEBUG: Fuzzy-matched search block at line {enum_i+1} (whitespace-tolerant)")
                    break
        
        # Pass 3: Markdown-normalized match — handles code fences, heading variations, collapsed whitespace
        if match_start == -1:
            import re as re_match
            
            def normalize_md(line):
                """Normalize a line for markdown-tolerant matching."""
                s = line.strip()
                # Normalize code fences: ```bash, ```python, etc → ```
                s = re_match.sub(r'^```\w*', '```', s)
                # Collapse multiple spaces/tabs into single space
                s = re_match.sub(r'\s+', ' ', s)
                # Normalize markdown headings: ##  Title → ## Title
                s = re_match.sub(r'^(#{1,6})\s+', r'\1 ', s)
                return s.lower()
            
            # Skip search lines that are LLM truncation artifacts
            clean_search_indices = []
            skip_patterns = ('...', '[...]', '# ...', '// ...', '/* ... */', '// rest of code', '/* rest of code */',
                            '...', '[CODE]', '[REST]', '[...]')
            for idx in range(search_lines_count):
                sl = str(s_map.get(int(idx), ""))
                stripped_sl = sl.strip()
                if stripped_sl not in skip_patterns:
                    clean_search_indices.append(int(idx))
            
            if len(clean_search_indices) >= 2:  # Need at least 2 real lines to match
                for enum_i in range(original_line_count - len(clean_search_indices) + 1):
                    matched = True
                    offset = 0
                    for ci_idx, search_idx in enumerate(clean_search_indices):
                        # Find the next matching content line starting from current position
                        found = False
                        search_idx_int = int(search_idx)
                        ci_idx_int = int(ci_idx)
                        search_norm = normalize_md(str(s_map.get(search_idx_int, "")))
                        if not search_norm:
                            continue
                        if isinstance(offset, int):
                            while int(enum_i) + offset + int(ci_idx_int) < original_line_count:
                                content_idx = int(enum_i) + offset + int(ci_idx_int)
                                content_norm = normalize_md(str(c_map.get(content_idx, "")).rstrip('\\r\\n'))
                                if content_norm == search_norm:
                                    found = True
                                    break
                                elif ci_idx_int == 0:
                                    # First line must match at current position
                                    break
                                else:
                                    offset += 1
                                    if offset > 3:  # Don't skip more than 3 lines
                                        break
                        if not found:
                            matched = False
                            break
                    if matched:
                        # Calculate the full match range from first to last matched line
                        first_search = 0
                        last_search = 0
                        for enum_x, x in enumerate(clean_search_indices):
                            if enum_x == 0:
                                first_search = x
                            last_search = x
                        match_start = int(enum_i)
                        # Recalculate search_lines to cover the full range in the file
                        # Keep original for replacement calculation
                        print(f"DEBUG: Markdown-normalized match at line {enum_i+1} (code fence / heading tolerant)")
                        break
        
        # Pass 4: Single-line partial substring match
        # If the LLM just gave us a snippet like "os.getenv('API_KEY')" without indentation
        if match_start == -1 and search_lines_count == 1:
            search_str = str(s_map.get(0, "")).strip()
            if len(search_str) > 5: # Don't replace tiny things like "()"
                # Find all lines containing this substring
                matches = []
                for i in range(original_line_count):
                    line = str(c_map.get(i, ""))
                    if search_str in line:
                        matches.append(int(i))
                
                # Only apply if we found exactly ONE unambiguous match
                if len(matches) == 1:
                    match_start_int = int(matches[0])
                    match_start = match_start_int
                    # We need to hack the replacement to only replace the substring within the line
                    original_line = str(c_map.get(match_start_int, ""))
                    new_line = original_line.replace(search_str, str(replace_text).strip())
                    
                    # Instead of rewriting the main loop logic, we just manually apply it here
                    # and continue to the next edit
                    c_map[match_start_int] = new_line
                    print(f"DEBUG: Substring match applied for single line at {match_start+1}")
                    continue
        
        # Pass 5: Multi-line anchor match — find the first search line in the file,
        # then replace from that anchor for the length of the search block.
        # This handles cases where the LLM's middle lines differ slightly.
        if match_start == -1 and search_lines_count >= 2:
            first_line = str(s_map.get(0, "")).strip()
            if len(first_line) > 5:
                anchors = []
                for i in range(original_line_count):
                    line = str(c_map.get(i, ""))
                    if line.strip() == first_line:
                        anchors.append(int(i))
                
                if len(anchors) == 1:
                    match_start = int(anchors[0])
                    print(f"DEBUG: Anchor match (first-line) at line {match_start+1} for {search_lines_count}-line block")
        
        if match_start == -1:
            print(f"DEBUG: Search block not found: {search[:50]}...")
            continue
        
        match_end = match_start + search_lines_count
        
        # Build the replacement lines (preserve file's line ending style)
        line_ending = '\n'
        if original_line_count > 0 and '\r\n' in c_map.get(0, ""):
            line_ending = '\r\n'
        
        replacement_lines = []
        for line in replace_text.splitlines():
            replacement_lines.append(line + line_ending)
        
        # SAFETY GUARD 2: Test-apply and check total file damage
        test_map = {}
        for enum_c in range(original_line_count):
            if int(enum_c) < match_start or int(enum_c) >= match_end:
                test_map[len(test_map)] = str(c_map.get(int(enum_c), ""))
            elif int(enum_c) == match_start:
                for rep in replacement_lines:
                    test_map[len(test_map)] = str(rep)
        
        lines_lost = original_line_count - len(test_map)
        # Allow removing up to 25 lines or 30% of the file, whichever is higher
        if lines_lost > max(25, int(original_line_count * 0.3)):
            print(f"DEBUG: BLOCKED catastrophic edit - would remove {lines_lost}/{original_line_count} total lines ({search[:60]}...)")
            continue
        
        c_map = test_map
        original_line_count = len(c_map)
        
        print(f"DEBUG: Applied edit at lines {match_start+1}-{match_end} ({search_lines_count} lines matched, {len(replacement_lines)} replacement lines)")
    
    return ''.join([str(c_map.get(i, "")) for i in range(original_line_count)])


def query_gemini(prompt, context="", temperature=0.4):
    if not GEMINI_API_KEY:
        return None, None
    headers = {'Content-Type': 'application/json'}
    final_prompt = f"""You are an autonomous GitHub bot called @mayo.
Context: {context}
Request: {prompt}
Instructions:
1. Be concise and summarize your thoughts into ONE comment if possible.
2. Do not reply to yourself unless absolutely necessary.
3. If writing code, return full files.
4. Focus on responding to other users if they reply to you."""
    
    payload = {
        "contents": [{"parts": [{"text": final_prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 16000}
    }
    try:
        r = requests.post(f"{GEMINI_API_URL}?key={GEMINI_API_KEY}", json=payload, headers=headers, timeout=120)
        r.raise_for_status()
        return r.json()['candidates'][0]['content']['parts'][0]['text'], "Gemini 2.5 Flash"
    except Exception as e:
        print(f"Gemini Error: {e}")
        return None, None

def query_gemini_for_code(prompt, context=""):
    code_prompt = f"""{prompt}
IMPORTANT: If suggestions involve file changes, respond options:
1. Normal text.
2. JSON for auto-apply:
```json
{{ "explanation": "...", "files": {{ "path/to/file": "content" }} }}
```"""
    content, _ = query_gemini(code_prompt, context)
    return content

# === API CALL HELPERS ===
def _try_fireworks_api(prompt, key, temperature, model="accounts/fireworks/models/llama-v3p3-70b-instruct"):
    if not key: return None, None
    headers = {'Content-Type': 'application/json'}
    fw_payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 4096
    }
    print(f"DEBUG: Trying Fireworks with model={model}, key_prefix={key[:10]}...")
    try:
        r = requests.post(
            "https://api.fireworks.ai/inference/v1/chat/completions",
            json=fw_payload,
            headers={**headers, 'Authorization': f'Bearer {key}'},
            timeout=120
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'], f"Fireworks AI (Llama 3.3 70B)"
    except requests.exceptions.HTTPError as e:
        try:
            err_json = r.json()
            err_body = err_json.get('error', {}).get('message', r.text[:500])
        except:
            err_body = r.text[:500] if r.text else "No response body"
        print(f"Fireworks HTTP {r.status_code}: {err_body}")
        return None, None
    except Exception as e:
        print(f"Fireworks API Error: {e}")
        return None, None

def _try_nvidia_nim_api(prompt, key, temperature, model="deepseek-ai/deepseek-v4-pro", max_tokens=16384, thinking=False):
    if not key: return None, None
    headers = {
        'Authorization': f'Bearer {key}',
        'Accept': 'application/json'
    }
    nim_payload = {
        "model": model,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 1.00,
        "stream": False
    }
    if thinking:
        nim_payload["chat_template_kwargs"] = {"thinking": True}
    
    # Retry up to 3 times for transient errors (500, 502, 503, timeout)
    for attempt in range(3):
        print(f"DEBUG: Trying NVIDIA NIM with model={model}, key_prefix={key[:10]}... attempt {attempt+1}/3")
        try:
            r = requests.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                json=nim_payload,
                headers=headers,
                timeout=300
            )
            if r.status_code >= 500:
                # Server error, retry
                print(f"NVIDIA NIM HTTP {r.status_code}, retrying...")
                time.sleep(5)
                continue
            r.raise_for_status()
            result = r.json()
            content = result['choices'][0]['message']['content']
            display = f"NVIDIA NIM ({model})"
            return content, display
        except requests.exceptions.HTTPError as e:
            try:
                err_json = r.json()
                err_body = err_json.get('error', {}).get('message', r.text[:500])
            except:
                err_body = r.text[:500] if r.text else "No response body"
            # Retry on 5xx errors
            if r.status_code >= 500:
                print(f"NVIDIA NIM HTTP {r.status_code}: {err_body}, retrying...")
                time.sleep(5)
                continue
            print(f"NVIDIA NIM HTTP {r.status_code}: {err_body}")
            return None, None
        except Exception as e:
            print(f"NVIDIA NIM API Error: {e}")
            if attempt < 2:
                time.sleep(5)
                continue
            return None, None
    return None, None

def _try_gemini_api(prompt, key, temperature, model="gemini-2.5-flash"):
    if not key: return None, None
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 8000}
    }
    try:
        r = requests.post(f"{GEMINI_API_URL}?key={key}", json=payload, headers=headers, timeout=120)
        r.raise_for_status()
        return r.json()['candidates'][0]['content']['parts'][0]['text'], f"Gemini 2.5 Flash ({key[:8]}...)"
    except requests.exceptions.HTTPError as e:
        err_body = r.text[:300] if r.text else "No response body"
        print(f"Gemini HTTP Error {r.status_code}: {err_body}")
        return None, None
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return None, None

# === TRIPLE-AI FUNCTIONS ===
def query_gemini_scanner(prompt, temperature=0.2):
    """Scanner AI — reads codebase, outputs text-only analysis. Uses NVIDIA NIM Gemma-4-31B, then Fireworks fallback."""
    if NVIDIA_API_KEY:
        content, model_name = _try_nvidia_nim_api(prompt, NVIDIA_API_KEY, temperature, model="google/gemma-4-31b-it", max_tokens=8192)
        if content: return content, model_name

    if GEMINI_FALLBACK_API_KEY:
        content, model_name = _try_fireworks_api(prompt, GEMINI_FALLBACK_API_KEY, temperature)
        if content: return content, model_name

    return None, None

def query_gemini_newcrons(prompt, temperature=0.2):
    """Dedicated Fireworks for cron phases. Uses Fireworks with GEMINI_NEWCRONS_API_KEY."""
    if GEMINI_NEWCRONS_API_KEY:
        content, model_name = _try_fireworks_api(prompt, GEMINI_NEWCRONS_API_KEY, temperature)
        if content: return content, model_name
    
    return None, None

GEMINI_EXECUTOR_API_KEY = os.environ.get('GEMINI_EXECUTOR_API_KEY')
GEMINI3_FALLBACK_API_KEY = os.environ.get('GEMINI3_FALLBACK_API_KEY')

def query_groq(prompt, api_key=None, temperature=0.1):
    """Executor AI (Llama 3.3 70B) — produces surgical code edits via Groq."""
    if api_key is None:
        api_key = GROK_API_KEY
    # On retry, rotate to GROK_FALLBACK_API_KEY if available
    retry_key = GROK_FALLBACK_API_KEY if GROK_FALLBACK_API_KEY and GROK_FALLBACK_API_KEY != api_key else api_key
    keys = [api_key, retry_key]
    
    # CRITICAL: Trim prompt to fit Groq's 6000 TPM limit (leave room for response)
    max_prompt_tokens = 4500
    if len(prompt) > max_prompt_tokens * 4:  # rough char to token estimate
        print(f"DEBUG: Trimming prompt from {len(prompt)} to {max_prompt_tokens * 4} chars for Groq")
        prompt = prompt[:max_prompt_tokens * 4] + "\n\n[TRUNCATED FOR TOKEN LIMIT]"
    
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 4096
    }
    for attempt in range(2):
        try:
            current_key = keys[attempt]
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {current_key}'
            }
            r = requests.post(GROQ_API_URL, json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            return r.json()['choices'][0]['message']['content'], f"Groq (llama-3.1-8b-instant)"
        except Exception as e:
            err_body = str(getattr(getattr(e, 'response', None), 'text', ''))
            key_preview = "".join([c for i, c in enumerate(str(current_key)) if i < 10])
            print(f"Groq/Llama Error (key {key_preview}... attempt {attempt+1}/2): {e} | {err_body}")
            if attempt < 1:
                print(f"DEBUG: Groq failed. Waiting 15s before retry with {'fallback' if keys[1] != api_key else 'same'} key...")
                time.sleep(15)
            else:
                return None, None
    return None, None

def query_gemini_executor(prompt, temperature=0.1):
    """Ultimate Fallback Executor AI (Gemini 2.5 Flash)."""
    if not GEMINI_EXECUTOR_API_KEY:
        print("DEBUG: GEMINI_EXECUTOR_API_KEY not set, skipping Gemini executor fallback.")
        return None, None
    
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 8000}
    }
    keys = [k for k in [GEMINI_EXECUTOR_API_KEY, GEMINI3_FALLBACK_API_KEY] if k]
    if not keys:
        print("DEBUG: No Gemini executor keys available.")
        return None, None
    
    for i, key in enumerate(keys):
        try:
            r = requests.post(f"{GEMINI_API_URL}?key={key}", json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            return r.json()['candidates'][0]['content']['parts'][0]['text'], f"Gemini 2.5 Flash"
        except Exception as e:
            err_body = str(getattr(getattr(e, 'response', None), 'text', ''))
            key_preview = "".join([c for i2, c in enumerate(str(key)) if i2 < 10])
            print(f"Gemini Executor Error (key {key_preview}...): {e} | {err_body}")
            if i < len(keys) - 1:
                print(f"DEBUG: Gemini Executor failed. Waiting 15s before trying fallback key...")
                time.sleep(15)
            else:
                return None, None
    return None, None

def query_fireworks_executor(prompt, temperature=0.1):
    """Executor AI — produces surgical code edits. Uses NVIDIA NIM DeepSeek V4 Pro, then Fireworks fallback."""
    if NVIDIA_API_KEY:
        content, model_name = _try_nvidia_nim_api(prompt, NVIDIA_API_KEY, temperature, model="deepseek-ai/deepseek-v4-pro", max_tokens=16384, thinking=True)
        if content: return content, model_name

    if not FIREWORKS_API_KEY:
        print("DEBUG: FIREWORKS_API_KEY not set, skipping Fireworks executor fallback.")
        return None, None
    
    headers = {'Content-Type': 'application/json'}
    model = "accounts/fireworks/models/llama-v3p3-70b-instruct"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 4096
    }
    try:
        r = requests.post(
            "https://api.fireworks.ai/inference/v1/chat/completions",
            json=payload,
            headers={**headers, 'Authorization': f'Bearer {FIREWORKS_API_KEY}'},
            timeout=120
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'], f"Fireworks AI ({model})"
    except Exception as e:
        err_body = str(getattr(getattr(e, 'response', None), 'text', ''))
        print(f"Fireworks Executor Error: {e} | {err_body}")
        return None, None

def query_gemini_reviewer(prompt, temperature=0.1):
    """Reviewer AI — validates edits, returns verdict JSON. Uses NVIDIA NIM Kimi K2.6, then Fireworks fallback."""
    if NVIDIA_API_KEY:
        content, model_name = _try_nvidia_nim_api(prompt, NVIDIA_API_KEY, temperature, model="moonshotai/kimi-k2.6", max_tokens=16384, thinking=True)
        if content: return content, model_name

    if FIREWORKS2_API_KEY:
        content, model_name = _try_fireworks_api(prompt, FIREWORKS2_API_KEY, temperature)
        if content: return content, model_name

    return None, None

def audit_pending_reviews(gh):
    """Reviewer checks PENDING REVIEW entries in memory and updates with actual PR status."""
    try:
        bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
        memory_file = bot_repo.get_contents("data/global_memory.md")
        memory = memory_file.decoded_content.decode('utf-8')
        
        if 'PENDING REVIEW' not in memory:
            print("DEBUG: No pending reviews to audit")
            return
        
        import re as re_mod
        pending_entries = re_mod.findall(r'\(Ref: (https://github\.com/[^\)]+/pull/\d+)\) - \*Status: PENDING REVIEW\*', memory)
        
        updated_memory = memory
        for pr_url in pending_entries:
            try:
                parts = pr_url.replace('https://github.com/', '').split('/pull/')
                repo_name = parts[0]
                pr_number = int(parts[1])
                
                repo = gh.get_repo(repo_name)
                pr = repo.get_pull(pr_number)
                
                if pr.merged:
                    status = "MERGED - Joseph approved!"
                elif pr.state == 'closed':
                    status = "REJECTED - Joseph closed this"
                elif pr.state == 'open':
                    continue  # Still open, skip
                else:
                    continue
                
                # Check for Joseph's comments
                comments = list(pr.get_issue_comments())
                joseph_comment = ""
                for c in comments:
                    if c.user.login != 'joe-gemini-bot[bot]':
                        joseph_comment = f" Comment: '{c.body[:80]}'"
                        break
                
                updated_memory = str(updated_memory).replace(
                    f"(Ref: {pr_url}) - *Status: PENDING REVIEW*",
                    f"(Ref: {pr_url}) - *Status: {status}{joseph_comment}*"
                )
                print(f"DEBUG: Updated review status for {pr_url}: {status}")
            except Exception as e:
                print(f"DEBUG: Failed to audit PR {pr_url}: {e}")
        
        # Memory Decay: Keep only the 30 most recent entries. Archive older ones.
        mem_lines = str(updated_memory).split('\n')
        repo_entries = []
        for line_idx, line in enumerate(mem_lines):
            if isinstance(line, str) and line.startswith('- **Repo:'):
                repo_entries.append(int(line_idx))
        
        if len(repo_entries) > 30:
            cutoff_idx = int(repo_entries[-30])
            header = mem_lines[0]
            archived_count = len(repo_entries) - 30
            summary_msg = f"\n- *[ARCHIVED] {archived_count} older lessons were archived to preserve focus.*"
            new_lines = [header, summary_msg]
            for m_idx, m_line in enumerate(mem_lines):
                if m_idx >= cutoff_idx:
                    new_lines.append(m_line)
            updated_memory = '\n'.join(new_lines)

        if updated_memory != memory:
            bot_repo.update_file(
                "data/global_memory.md",
                co_author_msg("chore(memory): archive old lessons"),
                updated_memory,
                memory_file.sha
            )
            print("DEBUG: Memory updated with review audits and decay")
    except Exception as e:
        print(f"DEBUG: Failed to audit pending reviews: {e}")

def update_ai_communication_log(gh, ts, scanner_summary, executor_proposal, reviewer_verdict):
    """Append a cycle entry to ai_communication.md and truncate to last 5 cycles."""
    try:
        bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
        try:
            comm_file = bot_repo.get_contents("data/ai_communication.md")
            if hasattr(comm_file, 'decoded_content') and comm_file.decoded_content:
                old_log = comm_file.decoded_content.decode('utf-8')
            else:
                import base64
                old_log = base64.b64decode(comm_file.content).decode('utf-8')
        except:
            old_log = ""
        
        entry = (
            f"\n## Cycle {ts}\n"
            f"**Scanner**: {scanner_summary}\n\n"
            f"**Executor**: {executor_proposal}\n\n"
            f"**Reviewer**: {reviewer_verdict}\n\n---\n"
        )
        
        new_log = old_log + entry
        
        # Truncate to last 5 cycles
        cycles = new_log.split('## Cycle ')
        if len(cycles) > 6:  # header + 5 cycles
            new_log = cycles[0] + '## Cycle '.join(cycles[-5:])
        
        bot_repo.update_file(
            "data/ai_communication.md",
            co_author_msg(f"feat(comms): log cycle {ts}"),
            new_log,
            comm_file.sha
        )
        print("DEBUG: AI communication log updated")
    except Exception as e:
        print(f"DEBUG: Failed to update communication log: {e}")

@app.route('/', methods=['GET'])
def home():
    return "Mayo Vercel Bot is Active! 🚀", 200
@app.route('/status', methods=['GET'])
def get_status():
    """Simple dashboard endpoint to check Mayo's health and memory."""
    try:
        integration = GithubIntegration(auth=AppAuth(APP_ID, PRIVATE_KEY))
        installations = integration.get_installations()
        if not installations or installations.totalCount == 0:
            return jsonify({'error': 'No installations found'})

        token = integration.get_access_token(installations[0].id).token
        gh = Github(auth=Token(token))
        bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
        
        # Get memory length
        mem_file = bot_repo.get_contents("data/global_memory.md")
        mem_content = mem_file.decoded_content.decode('utf-8')
        mem_lines = len(mem_content.split('\n'))
        
        # Get comms length
        comms_file = bot_repo.get_contents("data/ai_communication.md")
        comms_content = comms_file.decoded_content.decode('utf-8')
        cycles = len(comms_content.split('## Cycle ')) - 1
        
        return jsonify({
            'status': 'Online',
            'bot_name': 'Mayo 🤖',
            'memory_lines': mem_lines,
            'recent_cycles_logged': cycles,
            'uptime_seconds': int(time.time()),
            'excluded_repos': EXCLUDED_REPOS
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

 

@app.route('/health', methods=['GET'])
def health():
    """Diagnostic endpoint to verify environments."""
    status = {
        'status': 'Online',
        'env_checks': {
            'APP_ID': bool(APP_ID),
            'PRIVATE_KEY': bool(PRIVATE_KEY and 'BEGIN RSA PRIVATE KEY' in PRIVATE_KEY),
            'WEBHOOK_SECRET': bool(WEBHOOK_SECRET),
            'GEMINI_KEY': bool(GEMINI_API_KEY),
            'NVIDIA_KEY': bool(NVIDIA_API_KEY)
        },
        'bot_login': BOT_LOGIN_CACHE or 'Not cached yet'
    }
    return jsonify(status)

@app.route('/permissions', methods=['GET'])
def permissions_check():
    """Returns the current permissions and events Mayo is subscribed to."""
    try:
        integration = GithubIntegration(auth=AppAuth(APP_ID, PRIVATE_KEY))
        installations = integration.get_installations()
        results = []
        for inst in installations:
            token = integration.get_access_token(inst.id).token
            headers = {
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            # Check the installation settings
            resp = requests.get(f"https://api.github.com/app/installations/{inst.id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                results.append({
                    'installation_id': inst.id,
                    'permissions': data.get('permissions', {}),
                    'events': data.get('events', []),
                    'repositories_selection': data.get('repository_selection', 'unknown')
                })
            else:
                results.append({'installation_id': inst.id, 'error': resp.status_code, 'body': resp.text})
        return jsonify({'installations': results})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.errorhandler(500)
def handle_500(e):
    import traceback
    err = traceback.format_exc()
    print(f"SERVER CRASH (500): {err}")
    return jsonify({'error': 'Internal Server Error', 'traceback': err}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    is_valid, reason = verify_signature(request)
    if not is_valid:
        print(f"Webhook rejected: {reason}")
        return jsonify({'error': 'Invalid signature', 'reason': reason}), 401
    
    event_type = request.headers.get('X-GitHub-Event')
    payload = request.json
    
    try:
        if event_type == 'issue_comment' and payload.get('action') == 'created':
            handle_issue_comment(payload)
        elif event_type == 'pull_request' and payload.get('action') in ['opened', 'synchronize']:
             handle_pr(payload)
        elif event_type == 'pull_request_review' and payload.get('action') == 'submitted':
            handle_pr_review_feedback(payload)
    except Exception as e:
        import traceback
        print(f"WEBHOOK CRASH: {traceback.format_exc()}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

    return jsonify({'status': 'ok'})

def handle_pr_review_feedback(payload):
    """Record Joseph's review outcome in global memory."""
    try:
        pr_data = payload.get('pull_request', {})
        review = payload.get('review', {})
        pr_url = pr_data.get('html_url', '')
        review_state = review.get('state', 'commented').upper()  # APPROVED, CHANGES_REQUESTED, COMMENTED
        reviewer = review.get('user', {}).get('login', 'unknown')
        
        # Only process reviews from the repo owner, not the bot itself
        bot_login = get_bot_login()
        if reviewer == bot_login:
            return
        
        # Map review states to memory-friendly labels
        state_map = {
            'APPROVED': 'APPROVED - Joseph liked this!',
            'CHANGES_REQUESTED': 'REJECTED - Needs improvement',
            'COMMENTED': 'COMMENTED - Joseph had feedback',
            'DISMISSED': 'DISMISSED'
        }
        memory_status = state_map.get(review_state, review_state)
        
        installation = payload.get('installation')
        if not installation:
            return
        
        gh = get_github_client(installation['id'])
        bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
        
        memory_file = bot_repo.get_contents("data/global_memory.md")
        old_memory = memory_file.decoded_content.decode('utf-8')
        
        # Update the PENDING REVIEW status for this PR
        if pr_url in old_memory:
            new_memory = old_memory.replace(
                f"(Ref: {pr_url}) - *Status: PENDING REVIEW*",
                f"(Ref: {pr_url}) - *Status: {memory_status}*"
            )
            if new_memory != old_memory:
                bot_repo.update_file(
                    "data/global_memory.md",
                    co_author_msg(f"feat(memory): record review outcome ({review_state})"),
                    new_memory,
                    memory_file.sha
                )
                print(f"DEBUG: Memory updated with review: {review_state} for {pr_url}")
        else:
            print(f"DEBUG: PR {pr_url} not found in memory, skipping review update")
    except Exception as e:
        print(f"DEBUG: Failed to record review feedback: {e}")

def handle_pr(payload):
    """Handle PR opened/synchronized events."""
    bot_login = get_bot_login()
    # Don't review own PRs (if we ever create them)
    if payload.get('pull_request', {}).get('user', {}).get('login') == bot_login:
        return

    try:
        installation = payload.get('installation')
        if not installation:
            print("No installation in payload")
            return
        

        gh = get_github_client(installation['id'])
        repo_info = payload['repository']
        repo = gh.get_repo(repo_info['full_name'])
        pr_number = payload['pull_request']['number']
        pr = repo.get_pull(pr_number)
        bot_login = get_bot_login()
        
        # DEBUG: Verify we reached here
        print(f"DEBUG: Processing PR #{pr_number}")
        
        # Fetch memory
        try:
            memory = fetch_memory(repo, pr_number, bot_login)
            files_already_read = memory.get('files_read', [])
        except Exception as e:
            print(f"Memory fetch failed: {e}")
            files_already_read = []
        
        # Get repo structure
        try:
            repo_structure = get_repo_structure(repo)
        except Exception as e:
            print(f"Structure fetch failed: {e}")
            repo_structure = "(Structure fetch failed)"
        
        # Get the Diff
        diff_url = pr.diff_url
        diff_content = requests.get(diff_url).text
        
        if len(diff_content) > 60000:
            diff_content = diff_content[:60000] + "\n...(truncated)..."
        
        base_context = f"""
Repository Structure:
{repo_structure}

Files already read (from memory):
{', '.join(files_already_read) if files_already_read else 'None'}

PR Title: {pr.title}
PR Description: {pr.body}

Diff:
{diff_content}
"""
        
        # Step 1: Ask what files to read
        needed_files = get_context_expansion_files(f"Review this PR: {pr.title}", base_context)
        
        expanded_context = base_context
        new_files_read = []
        
        if needed_files and isinstance(needed_files, list):
            ftr_filtered = [f for f in needed_files if f not in files_already_read]
            files_to_read = [f for f_idx, f in enumerate(ftr_filtered) if f_idx < 5]
            
            if files_to_read:
                file_contents_acc = ""
                for file_path in files_to_read:
                    if ".." in file_path or file_path.startswith("/"):
                        continue
                    content = read_file_content(repo, file_path)
                    if isinstance(content, str):
                        file_contents_acc += f"\n--- {file_path} ---\n{content}\n"
                        new_files_read.append(file_path)
                
                if file_contents_acc:
                    expanded_context += f"\n\nFile Contents:\n{file_contents_acc}"
        
        # Parse diff for file/line info
        diff_files = parse_diff_files(diff_content)
        file_line_info = ""
        for df in diff_files:
            lines_list = df.get('lines')
            if isinstance(lines_list, list):
                ranges = ", ".join([f"L{r.get('start', '?')}-{r.get('end', '?')}" for r in lines_list if isinstance(r, dict)])
                file_line_info += f"  {df.get('path', 'unknown')}: {ranges}\n"
        
        # Step 2: Generate review with committable suggestions
        prompt = f"""You are an expert Principal Software Engineer. 
Perform a RIGOROUS technical review of this PR.

Changed files and line ranges:
{file_line_info}

Context:
{expanded_context}

IMPORTANT: Respond ONLY with valid JSON in this exact format:
{{
  "summary": "Full technical report (Markdown). Analyze architecture, security, performance, race conditions, and error handling. Be critical and thorough.",
  "suggestions": [
    {{
      "file": "path/to/file.ext",
      "line": 42,
      "original": "the exact original line(s) from the diff",
      "replacement": "your suggested replacement code",
      "reason": "Detailed justification (e.g. complexity reduction, security fix)"
    }}
  ]
}}

Rules:
1. SUMMARY MUST BE DEEP. Not just "looks good". Analyze impact.
2. SUGGESTIONS are OPTIONAL. Only provide if necessary/high-value.
3. "line" must be the END line number in the new file (right side of diff)
4. "original" must be the EXACT code currently at that line
5. "replacement" is your suggested fix
6. Do NOT suggest formatting/style changes unless critical.
7. Do NOT wrap in markdown code blocks, return raw JSON only
"""
        review_raw, reviewer_model = query_gemini(prompt, temperature=0.2)
        
        all_files_for_mem = []
        if isinstance(files_already_read, list):
            all_files_for_mem += files_already_read
        if isinstance(new_files_read, list):
            all_files_for_mem += new_files_read
            
        memory_block = format_memory_block({"files_read": all_files_for_mem})
        
        # Try to parse as structured suggestions
        suggestions_data = None
        if review_raw:
            try:
                # Try direct JSON parse first
                suggestions_data = json.loads(review_raw)
            except json.JSONDecodeError:
                # Try extracting from markdown code block
                suggestions_data = extract_json_from_response(review_raw)
        
        if suggestions_data and isinstance(suggestions_data, dict) and 'suggestions' in suggestions_data:
            summary = suggestions_data.get('summary', 'Code review complete.')
            suggestions = suggestions_data['suggestions'] or []
            
            # Build inline review comments
            review_comments = []
            for enum_s, s in enumerate(suggestions):
                if enum_s >= 5: break
                file_path = s.get('file', '')
                line_num = s.get('line', 0)
                original = s.get('original', '')
                replacement = s.get('replacement', '')
                reason = s.get('reason', '')
                
                if not file_path or not line_num or not replacement:
                    continue
                
                # Build committable suggestion body
                body = f"{reason}\n\n```suggestion\n{replacement}\n```"
                
                review_comments.append({
                    'path': file_path,
                    'line': int(line_num),
                    'body': body
                })
            
            if review_comments:
                try:
                    # Use GitHub REST API directly for line+side support
                    token = get_installation_token(installation['id'])
                    api_url = f"https://api.github.com/repos/{repo_info['full_name']}/pulls/{pr_number}/reviews"
                    model_display = f" ({reviewer_model})" if reviewer_model else ""
                    review_payload = {
                        'body': f"🤖 **Automated Code Review{model_display}**\n\n{summary}{memory_block}",
                        'event': 'COMMENT',
                        'comments': [{
                            'path': c['path'],
                            'line': c['line'],
                            'side': 'RIGHT',
                            'body': c['body']
                        } for c in review_comments]
                    }
                    api_headers = {
                        'Authorization': f'token {token}',
                        'Accept': 'application/vnd.github.v3+json'
                    }
                    resp = requests.post(api_url, json=review_payload, headers=api_headers)
                    if resp.status_code in [200, 201]:
                        print(f"Posted review with {len(review_comments)} inline suggestions")
                    else:
                        print(f"Review API failed: {resp.status_code} {resp.text}")
                        raise Exception(f"API {resp.status_code}")
                except Exception as review_err:
                    print(f"Review API exception: {review_err}")
                    # DISABLED: Review comments trigger email to PR participants
                    # pr.create_issue_comment(f"{fallback}{memory_block}")
                    pass
            else:
                # No inline suggestions, just post summary
                model_display = f" ({reviewer_model})" if reviewer_model else ""
                # DISABLED: Review comments trigger email
                # pr.create_issue_comment(f"🤖 **Automated Code Review{model_display}**\n\n{summary}{memory_block}")
                pass
        elif review_raw:
            # Gemini didn't return structured JSON, post as plain review
            model_display = f" ({reviewer_model})" if reviewer_model else ""
            # DISABLED: Review comments trigger email
            # pr.create_issue_comment(f"🤖 **Automated Code Review{model_display}**\n\n{review_raw}{memory_block}")
            pass
            
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        print(f"Error reviewing PR: {err_msg}")
        try:
            # Try to report error to user if possible
            if 'pr' in locals():
                # DISABLED: Error comment triggers email
                # pr.create_issue_comment(f"⚠️ **Bot Error**: Something went wrong.\n\n```\n{err_msg}\n```")
                pass
        except:
            pass

def handle_issue_comment(payload):
    # 0. Robust Early Initialization
    installation = payload.get('installation')
    issue_obj = None
    try:
        if not installation:
            print("ERROR: No installation in payload")
            return

        gh = get_github_client(installation['id'])
        repo_info = payload['repository']
        repo = gh.get_repo(repo_info['full_name'])
        comment = payload['comment']
        issue_number = payload['issue']['number']
        issue_obj = repo.get_issue(number=issue_number)
        
        bot_login = get_bot_login()
        
        # CRITICAL: Do not reply to self!
        if comment.get('user', {}).get('login') == bot_login:
            return

        body = comment.get('body', '').lower()
        
        # CRITICAL SECURITY: Ignore own comments (Double Check)
        if comment.get('user', {}).get('login') == bot_login:
            return
        if comment.get('user', {}).get('type') == 'Bot':
            return
        if '<!-- [memory]' in body or '<!-- [memory]' in comment.get('body', ''):
            return

        # Check mentions & replies
        mentioned = False
        if "mayo" in body.lower() or "joe-gemini" in body.lower():
            mentioned = True
        else:
            try:
                comments = list(issue_obj.get_comments())
                if len(comments) > 1:
                    last_comment = comments[-1]
                    prev_comment = comments[-2]
                    if str(last_comment.id) == str(comment.get('id')):
                        if prev_comment.user.login == bot_login:
                             mentioned = True
            except: pass
        
        if not mentioned: return

        # 1. Fetch Memory with [MEMORY] blocks
        memory = fetch_memory(repo, issue_number, bot_login)
        files_already_read = memory.get('files_read', [])
        
        # 2. Get repo structure
        repo_structure = get_repo_structure(repo)
        
        # 3. PR Context if applicable
        issue_context = f"Issue Title: {issue_obj.title}\nIssue Body:\n{issue_obj.body}\n"
        pr_context = ""
        if issue_obj.pull_request:
            try:
                pr = repo.get_pull(issue_number)
                diff_content = requests.get(pr.diff_url).text[:20000]
                pr_context = f"This is a pull request.\nPR Title: {pr.title}\nDiff:\n{diff_content}"
            except: pass
    
        # Get preceding comments for context
        comment_history = ""
        try:
            all_comments = list(issue_obj.get_comments())
            # Get up to 5 preceding comments, excluding the current one
            filtered_comments = [c for c in all_comments if str(c.id) != str(comment.get('id'))]
            recent_comments = [c for c_idx, c in enumerate(filtered_comments) if c_idx >= len(filtered_comments) - 5]
            if recent_comments:
                comment_history = "\nRecent Conversation History:\n"
                for c in recent_comments:
                    comment_history += f"@{c.user.login}: {c.body}\n---\n"
        except: pass
        
        base_context = f"""
Repository Structure:
{repo_structure}

Files already read (from memory):
{', '.join(files_already_read) if files_already_read else 'None'}

{issue_context}
{pr_context}
{comment_history}
"""
        
        # 4. Ask Gemini what files it needs
        needed_files = get_context_expansion_files(comment['body'], base_context)
        
        expanded_context = base_context
        new_files_read = []
        
        if needed_files and isinstance(needed_files, list):
            ftr_filtered = [f for f in needed_files if f not in files_already_read]
            files_to_read = [f for f_idx, f in enumerate(ftr_filtered) if f_idx < 5]
            
            if files_to_read:
                issue_obj.create_comment(f"👀 Checking: `{', '.join(files_to_read)}`...")
                
                file_contents = ""
                for file_path in files_to_read:
                    if ".." in file_path or file_path.startswith("/"):
                        continue
                    content = read_file_content(repo, file_path)
                    if content:
                        file_contents += f"\n--- {file_path} ---\n{content}\n"
                        new_files_read.append(file_path)
                
                expanded_context += f"\n\nFile Contents:\n{file_contents}"
        
        # 5. Generate response (Reviewer)
        reviewer_prompt = f"""You are Mayo, the Senior Quality Assurance & Reviewer AI.
Joseph (the human owner) has made a comment: "{comment['body']}"

Context:
{expanded_context}

Instructions:
1. Address Joseph's comment clearly and concisely.
2. If Joseph is providing feedback, rejecting something, or explaining a preferred pattern, state clearly how you understand his instruction.
3. If Joseph's request requires code changes, explain the technical plan for the fix and end your ENTIRE response with the exact marker: [REQUIRES_EXECUTION]
"""
        plan, reviewer_model = query_gemini_reviewer(reviewer_prompt)
        if not plan:
            issue_obj.create_comment("⚠️ **Reviewer (Mayo) Error:** I failed to generate a technical plan after multiple attempts. Please re-trigger me or check my API health.")
            return
        
        all_files_for_mem = []
        if isinstance(files_already_read, list):
            all_files_for_mem += files_already_read
        if isinstance(new_files_read, list):
            all_files_for_mem += new_files_read
            
        memory_block = format_memory_block({"files_read": all_files_for_mem})
        
        model_display = f" ({reviewer_model})" if reviewer_model else ""
        issue_obj.create_comment(f"🛡️ **Reviewer (Mayo){model_display}:**\n{plan}{memory_block}")
        
        # Save Joseph's feedback to memory
        try:
            bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
            mem_file = bot_repo.get_contents("data/global_memory.md")
            old_mem = mem_file.decoded_content.decode('utf-8')
            feedback_note = f"\n- **Joseph's Feedback on {repo.name}#{issue_number}**: \"{comment['body'][:120]}\" — Mayo acknowledged and responded."
            bot_repo.update_file(
                "data/global_memory.md",
                co_author_msg(f"feat(memory): save Joseph's feedback on {repo.name}#{issue_number}"),
                old_mem + feedback_note,
                mem_file.sha
            )
        except Exception as e:
            print(f"DEBUG: Failed to save feedback to memory: {e}")
        
        # 6. Execute Code Changes (Executor)
        if "[REQUIRES_EXECUTION]" in plan:
            used_model = None
            executor_model_display = f" ({used_model})" if used_model else ""
            issue_obj.create_comment(f"⚡ *Executor{executor_model_display} is now writing the code changes...*")
            
            # Re-read the exact file contents for the Executor (with clear delimiters)
            exact_files_context_list = []
            files_for_executor = []
            if isinstance(new_files_read, list) and new_files_read:
                files_for_executor = new_files_read
            elif isinstance(files_already_read, list):
                for f_idx, f in enumerate(files_already_read):
                    if f_idx < 3:
                        files_for_executor.append(f)
            
            for fp in files_for_executor:
                fc = read_file_content(repo, fp)
                if isinstance(fc, str):
                    # Truncate to avoid 413 Payload Too Large on Groq
                    if len(fc) > 7000:
                        fc_trunc = []
                        for char_idx, char in enumerate(fc):
                            if char_idx < 7000:
                                fc_trunc.append(char)
                            else:
                                break
                        fc = "".join(fc_trunc) + "\n...[TRUNCATED FOR LENGTH]..."
                    exact_files_context_list.append(f"\n--- START OF FILE: {fp} ---\n{fc}\n--- END OF FILE: {fp} ---\n")
            
            exact_files_context = "".join(exact_files_context_list)
            
            executor_prompt = f"""You are Mayo, the Executor AI (Surgical Code Engineer).
The Reviewer AI has established this plan based on Joseph's feedback:
{plan}

Repository: {repo.full_name}
Available files: {', '.join(files_for_executor)}

{exact_files_context}

Generate surgical search/replace edits to fulfill this plan.

HARD RULES:
1. FULL CONTEXT EDITS: You are fully authorized to replace entire functions, large sections of code, or update multiple files across the repository.
2. EXACT MATCH: The "search" field must be an EXACT copy of the original code from the file contents above. Character-for-character, including indentation.
3. NO PLACEHOLDERS: Never use "...", "// rest of code", or "# code remains".
4. NO UNINTENDED DELETIONS: Ensure you are not accidentally deleting important surrounding context.
5. PRESERVE EVERYTHING: Indentation, comments, blank lines outside your edit MUST remain untouched.
6. COPY DIRECTLY: Copy the search text DIRECTLY from the file contents shown above. Do NOT retype from memory.
7. NEW FILES: If you need to CREATE a new file that doesn't exist yet, use an EMPTY "search" field ("") and put the ENTIRE file content in "replace".
8. JSON ESCAPING: You MUST strictly escape all newlines (`\n`) and quotes (`\"`) inside your JSON string values! NEVER use literal line breaks inside the string. "json.loads" will crash if you do.

OUTPUT FORMAT (Strict JSON, nothing else):
{{
  "title": "[TYPE] Brief technical title",
  "body": "What was fixed and why.",
  "edits": [
    {{
      "file": "path/to/existing_file",
      "search": "EXACT lines from original (max 10 lines)",
      "replace": "Your improved replacement"
    }},
    {{
      "file": "path/to/new_file.yml",
      "search": "",
      "replace": "Full content of the new file here"
    }}
  ]
}}
            """
            # Run Fireworks AI first (most reliable), then Groq, then Gemini
            # Fireworks is most reliable - use it first
            fireworks_resp, fireworks_model = query_fireworks_executor(executor_prompt)
            fireworks_data = extract_json_from_response(fireworks_resp) if fireworks_resp else None
            used_model = fireworks_model or "Fireworks AI"
            
            if fireworks_data and isinstance(fireworks_data, dict) and 'edits' in fireworks_data:
                final_payload = fireworks_data
            else:
                # Try Groq dual keys
                executor1_resp, _ = query_groq(executor_prompt, api_key=os.environ.get('GROQ_API_KEY'))
                executor2_resp, _ = query_groq(executor_prompt, api_key=os.environ.get('GROK_2ND_EXECUTOR_API_KEY'))
                
                data1 = extract_json_from_response(executor1_resp) if executor1_resp else None
                data2 = extract_json_from_response(executor2_resp) if executor2_resp else None
                
                if data1 and isinstance(data1, dict) and 'edits' in data1:
                    final_payload = data1
                    used_model = "Groq (llama-3.1-8b-instant)"
                    if data2 and isinstance(data2, dict) and 'edits' in data2:
                        final_payload['edits'] = final_payload.get('edits', []) + data2.get('edits', [])
                elif data2 and isinstance(data2, dict) and 'edits' in data2:
                    final_payload = data2
                    used_model = "Groq (llama-3.1-8b-instant)"
                else:
                    # Try Groq fallback key
                    fb1_resp, _ = query_groq(executor_prompt, api_key=os.environ.get('GROK_FALLBACK_API_KEY'))
                    final_payload = extract_json_from_response(fb1_resp) if fb1_resp else None
                    
                    if not isinstance(final_payload, dict) or 'edits' not in final_payload:
                        # Use Fireworks if not already tried
                        if not fireworks_data:
                            final_payload = fireworks_data
                        
                        # Gemini as last resort
                        if not isinstance(final_payload, dict) or 'edits' not in final_payload:
                            fb2_resp, fb2_model = query_gemini_executor(executor_prompt)
                            used_model = fb2_model or used_model
                            if fb2_resp:
                                final_payload = extract_json_from_response(fb2_resp)
            
            if isinstance(final_payload, dict) and isinstance(final_payload.get('edits'), list):
                # Group edits by file
                file_edits = {}
                for edit in final_payload['edits']:
                    if not isinstance(edit, dict): continue
                    fpath = edit['file'] if 'file' in edit else None
                    if not fpath: continue
                    if fpath not in file_edits:
                        file_edits[fpath] = []
                    file_edits[fpath].append(edit)
                
                # Apply edits
                file_changes = {}
                failed_edits = []
                for fpath, edits in file_edits.items():
                    content = read_file_content(repo, fpath)
                    if not content:
                        # File doesn't exist — check if the executor wants to CREATE it
                        # (search is empty/missing but replace has full content)
                        new_file_content = ""
                        for edit in edits:
                            replace_val = edit.get('replace', '')
                            search_val = edit.get('search', '')
                            if replace_val and (not search_val or search_val.strip() == ''):
                                new_file_content += replace_val
                        if new_file_content:
                            file_changes[fpath] = new_file_content
                            print(f"DEBUG: New file will be created: {fpath}")
                        else:
                            failed_edits.append(f"`{fpath}`: file not found or empty")
                        continue
                    new_content = apply_surgical_edits(content, edits)
                    if new_content != content:
                        file_changes[fpath] = new_content
                    else:
                        for edit in edits:
                            search_preview = (edit.get('search', ''))[:80].replace('\n', '\\n')
                            failed_edits.append(f"`{fpath}`: `{search_preview}...`")
                
                if file_changes:
                    # Decide branch
                    if issue_obj.pull_request:
                        try:
                            pr = repo.get_pull(issue_number)
                            branch = pr.head.ref
                        except:
                            branch = f"mayo/fix-{issue_number}-{int(time.time())}"
                    else:
                        branch = f"mayo/fix-{issue_number}-{int(time.time())}"
                    
                    commit_title = final_payload.get('title', f"Fix: Addressed feedback in #{issue_number}")
                    success, commit_err = commit_changes_via_api(repo, branch, file_changes, commit_title)
                    executor_model_display = f" (via {used_model})" if used_model else ""
                    if success:
                        msg = f"✅ Committed changes to `{branch}`{executor_model_display}.\n\nDescription: {final_payload.get('body', commit_title)}"
                        if failed_edits:
                            safe_preview = [f"- {fe}" for i, fe in enumerate(failed_edits) if i < 5]
                            msg += f"\n\n⚠️ Some edits failed to match:\n" + "\n".join(safe_preview)
                        issue_obj.create_comment(msg)
                        
                        if not issue_obj.pull_request:
                            try:
                                repo.create_pull(title=commit_title, body=f"Automated fix.\n{final_payload.get('body', '')}", head=branch, base=repo.default_branch)
                                issue_obj.create_comment(f"🚀 Created new PR for `{branch}`")
                            except Exception as e:
                                print(f"PR Creation error: {e}")
                    else:
                        issue_obj.create_comment(f"⚠️ Executor generated edits, but the commit failed. Check logs.\n\n```\n{commit_err}\n```")
                else:
                    safe_preview = [f"- {fe}" for i, fe in enumerate(failed_edits) if i < 5]
                    
                    debug_info = "\n".join(safe_preview) if failed_edits else "No debug info available"
                    issue_obj.create_comment(f"⚠️ Executor generated edits, but none matched the file contents.\n\n**Failed search blocks:**\n{debug_info}\n\n*Retrying on next trigger.*")
            else:
                 issue_obj.create_comment("⚠️ Executor failed to generate valid JSON edits.")
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        print(f"Error processing comment: {e}")
        try:
            # Fallback reporting: If issue_obj is set, use it. 
            # If not, try to recreate the client to report the crash.
            if issue_obj:
                issue_obj.create_comment(f"⚠️ **Mayo Internal Webhook Error:**\n\n```python\n{err_msg}\n```")
            elif installation:
                temp_gh = get_github_client(installation['id'])
                temp_repo = temp_gh.get_repo(payload['repository']['full_name'])
                temp_issue = temp_repo.get_issue(payload['issue']['number'])
                temp_issue.create_comment(f"⚠️ **Mayo Initialization Crash:**\n\n```python\n{err_msg}\n```")
        except:
            pass

if __name__ == '__main__':
    app.run(port=3000)


