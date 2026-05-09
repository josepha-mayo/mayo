import os
import json
import time
import random
import requests
from github import Github, GithubIntegration
from github.Auth import AppAuth, Token

# Add the api directory to sys.path so we can import the core functions
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), 'api'))

# Import the core AI and GitHub functions from index.py
from index import (
    APP_ID, PRIVATE_KEY, GEMINI_API_URL, GEMINI_API_KEY, GROK_API_KEY, GROQ_API_URL, GEMINI2_API_KEY,
    audit_pending_reviews, get_repo_structure, read_file_content, query_gemini_scanner,
    query_groq, extract_json_from_response, apply_surgical_edits, query_gemini_reviewer,
    commit_changes_via_api, update_ai_communication_log, query_gemini_newcrons,
    query_fireworks_executor, query_gemini_executor, EXCLUDED_REPOS
)

def co_author_msg(msg):
    co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
    co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
    if co_author_name and co_author_email:
        return f"{msg}\n\nCo-authored-by: {co_author_name} <{co_author_email}>"
    return msg

def run_cron():
    print("DEBUG: Cron triggered — Triple-AI Pipeline (GitHub Actions)")
    
    try:
        # Initialize GitHub App
        integration = GithubIntegration(auth=AppAuth(APP_ID, PRIVATE_KEY))
        installations = integration.get_installations()

        if not installations or installations.totalCount == 0:
            print("DEBUG: No installations found")
            return

        installation = installations[0]
        token = integration.get_access_token(installation.id).token
        gh = Github(auth=Token(token))
        
        # === PHASE 0: REVIEWER AUDITS PENDING REVIEWS ===
        print("DEBUG: Phase 0 — Auditing pending reviews")
        audit_pending_reviews(gh)

        # === PHASE 0.5: CHECK APPROVED ISSUES ===
        print("DEBUG: Phase 0.5 — Checking for approved issues")
        try:
            bot_repo_name = os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo')
            bot_repo = gh.get_repo(bot_repo_name)
            mem_file = bot_repo.get_contents("data/global_memory.md")
            mem_content = mem_file.decoded_content.decode('utf-8')

            import re as re_mod
            awaiting_entries = re_mod.findall(
                r'\(Ref: (https://github\.com/([^/]+/[^/]+)/issues/(\d+))\) - \*Status: AWAITING JOSEPH\'S INPUT\*',
                mem_content
            )

            for issue_url, repo_name, issue_num in awaiting_entries:
                try:
                    issue_repo = gh.get_repo(repo_name)
                    issue = issue_repo.get_issue(int(issue_num))
                    
                    if issue.state == 'closed':
                        print(f"DEBUG: Issue {issue_url} is closed. Marking as resolved in memory.")
                        mem_content = mem_content.replace(f"(Ref: {issue_url}) - *Status: AWAITING JOSEPH'S INPUT*", f"(Ref: {issue_url}) - *Status: RESOLVED (Closed)*")
                        bot_repo.update_file("data/global_memory.md", co_author_msg("chore(memory): mark closed issue as resolved"), mem_content, mem_file.sha)
                        mem_file = bot_repo.get_contents("data/global_memory.md") # refresh sha
                        continue
                    
                    # check if the repo owner (or i) replied with instructions/approval
                    joseph_approved = False
                    joseph_reply = ""
                    repo_owner_login = issue_repo.owner.login
                    
                    for comment in issue.get_comments():
                        if comment.user.login not in ('joe-gemini-bot[bot]', 'github-actions[bot]'):
                            # Process any comment from the repo owner or Joseph as an instruction
                            if comment.user.login == repo_owner_login or comment.user.login == 'HOLYKEYZ':
                                joseph_approved = True
                                joseph_reply = comment.body
                                break
                    
                    if not joseph_approved:
                        continue
                    
                    print(f"DEBUG: Processed owner comment on issue {issue_url} — executing!")
                    
                    # Extract the Scanner's original analysis from the issue body
                    scanner_context = issue.body or ""
                    
                    # Gather the repo's code context for the Executor
                    structure = get_repo_structure(issue_repo, max_depth=2)
                    source_files = []
                    try:
                        contents = issue_repo.get_contents("")
                        EXCLUDED_FILES = ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lockb', '.min.js', '.min.css']
                        for item in contents:
                            if item.type == 'file' and any(item.name.endswith(ext) for ext in ['.py', '.js', '.ts', '.go', '.c', '.cpp', '.h', '.md', '.json']):
                                if not any(excl in item.name for excl in EXCLUDED_FILES):
                                    source_files.append(item.path)
                    except:
                        pass
                    
                    random.shuffle(source_files)
                    target_paths = source_files[:3]  # Reduced from 7 to 3 to fit tighter payload limits
                    file_contents = ""
                    for tp in target_paths:
                        content = read_file_content(issue_repo, tp)
                        if content:
                            # Truncate to avoid 413 Payload Too Large on Groq
                            if len(content) > 4000:
                                content = content[:4000] + "\n...[TRUNCATED FOR LENGTH]..."
                            file_contents += f"\n--- {tp} ---\n{content}\n"
                    
                    if not file_contents:
                        print(f"DEBUG: Could not read files for approved issue {issue_url}")
                        continue
                    
                    ts = int(time.time())
                    target_path_display = ", ".join(target_paths)
                    
                    # Run Executor with my approval context
                    executor_prompt = (
                        f"You are the EXECUTOR — a Senior Code Engineer.\n"
                        f"Joseph has approved this change via GitHub Issue: {issue.title}\n\n"
                        f"Joseph's reply: \"{joseph_reply}\"\n\n"
                        f"Original Scanner analysis:\n{scanner_context}\n\n"
                        f"Repo: {issue_repo.full_name}\nFiles: {target_path_display}\n"
                        f"Repo structure:\n{structure}\n\n"
                        f"File contents:\n{file_contents}\n\n"
                        f"Execute the approved change. Output strict JSON:\n"
                        f'{{"title": "[TYPE] Brief title", "body": "Description", '
                        f'"branch_name": "bot/issue-{issue_num}-{ts}", '
                        f'"edits": [{{"file": "path", "search": "exact original", "replace": "replacement"}}]}}'
                    )
                    
                    print("DEBUG: Executing Phase 0.5 Dual Groq Llama 3.3 + Fallbacks...")
                    executor1_resp, executor1_model = query_groq(executor_prompt, api_key=os.environ.get('GROK_API_KEY'))
                    executor2_resp, executor2_model = query_groq(executor_prompt, api_key=os.environ.get('GROK_2ND_EXECUTOR_API_KEY'))
                    
                    data1 = extract_json_from_response(executor1_resp) if executor1_resp else None
                    data2 = extract_json_from_response(executor2_resp) if executor2_resp else None
                    
                    improvement_data = None
                    used_model = executor1_model or executor2_model or "Groq (llama-3.1-8b-instant)"
                    
                    if (data1 and 'edits' in data1) or (data2 and 'edits' in data2):
                        improvement_data = data1 if data1 else data2
                        if data1 and data2 and 'edits' in data1 and 'edits' in data2:
                            improvement_data['edits'].extend(data2.get('edits', []))
                    else:
                        print("DEBUG: Phase 0.5 Primary Executors failed. Checking fallbacks...")
                        fb1_resp, _ = query_groq(executor_prompt, api_key=os.environ.get('GROK_FALLBACK_API_KEY'))
                        improvement_data = extract_json_from_response(fb1_resp) if fb1_resp else None
                        
                        if not improvement_data or 'edits' not in improvement_data:
                            fb2_resp, fb2_model = query_gemini_executor(executor_prompt)
                            used_model = fb2_model or used_model
                            if fb2_resp:
                                improvement_data = extract_json_from_response(fb2_resp)
                    
                    if improvement_data and 'edits' in improvement_data:
                            file_edits = {}
                            for edit in improvement_data['edits']:
                                fpath = edit.get('file') or (target_paths[0] if target_paths else '')
                                if fpath not in file_edits:
                                    file_edits[fpath] = []
                                file_edits[fpath].append(edit)
                            
                            file_changes = {}
                            for fpath, edits in file_edits.items():
                                content = read_file_content(issue_repo, fpath)
                                if not content:
                                    continue
                                new_content = apply_surgical_edits(content, edits)
                                if new_content != content:
                                    file_changes[fpath] = new_content
                            
                            if file_changes:
                                branch = improvement_data.get('branch_name', f'bot/issue-{issue_num}-{ts}')
                                title = improvement_data.get('title', f'Fix from approved issue #{issue_num}')
                                
                                success, err = commit_changes_via_api(issue_repo, branch, file_changes, title)
                                if success:
                                    co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
                                    co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
                                    co_author_line = f"\n\nCo-authored-by: {co_author_name} <{co_author_email}>" if co_author_name and co_author_email else ""
                                    pr = issue_repo.create_pull(
                                        title=f"[DRAFT] {title}",
                                        body=f"Approved by Joseph in {issue_url}\n\n{improvement_data.get('body', '')}\n\n*Executed by Mayo 🤖*{co_author_line}\n\n**This is a DRAFT PR — review and merge when ready.**",
                                        head=branch,
                                        base=issue_repo.default_branch,
                                        draft=True
                                    )
                                    print(f"DEBUG: PR created from approved issue: {pr.html_url}")

                                    # DISABLED: Closing issue triggers email to watchers
                                    # issue.create_comment(f"✅ Done! PR created: {pr.html_url}")
                                    # issue.edit(state='closed')

                                    # Update memory
                                    mem_file = bot_repo.get_contents("data/global_memory.md")
                                    mem = mem_file.decoded_content.decode('utf-8')
                                    mem = mem.replace(
                                        f"(Ref: {issue_url}) - *Status: AWAITING JOSEPH'S INPUT*",
                                        f"(Ref: {issue_url}) - *Status: EXECUTED → {pr.html_url}*"
                                    )
                                    bot_repo.update_file("data/global_memory.md", co_author_msg(f"feat(memory): executed approved issue on {repo_name}"), mem, mem_file.sha)
                except Exception as e:
                    print(f"DEBUG: Error processing approved issue {issue_url}: {e}")
        except Exception as e:
            print(f"DEBUG: Phase 0.5 error: {e}")

        # Get all repos via REST API
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        repos_response = requests.get('https://api.github.com/installation/repositories', headers=headers)
        repos_data = repos_response.json()
        repo_names = [r['full_name'] for r in repos_data.get('repositories', [])]
        print(f"DEBUG: Found {len(repo_names)} repos: {repo_names}")
        
        if not repo_names:
            print("No repos found")
            return
            
        # === TIMING HELPER ===
        def should_run_timed_phase(memory_text, phase_key, interval_hours):
            """Check if enough time has passed since last run of this phase."""
            import re as re_mod
            match = re_mod.search(rf'{phase_key}=(\d+)', memory_text)
            if not match:
                return True
            last_run = int(match.group(1))
            return (int(time.time()) - last_run) >= interval_hours * 3600
        
        def update_phase_timestamp(bot_repo_obj, memory_text, phase_key):
            """Update the timestamp for a timed phase in memory."""
            import re as re_mod
            ts_now = str(int(time.time()))
            if f'{phase_key}=' in memory_text:
                new_mem = re_mod.sub(rf'{phase_key}=\d+', f'{phase_key}={ts_now}', memory_text)
            else:
                new_mem = memory_text + f'\n<!-- {phase_key}={ts_now} -->'
            try:
                mem_file = bot_repo_obj.get_contents("data/global_memory.md")
                timing_msg = f"chore(timing): update {phase_key}"
                co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
                co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
                if co_author_name and co_author_email:
                    timing_msg = f"{timing_msg}\n\nCo-authored-by: {co_author_name} <{co_author_email}>"
                bot_repo_obj.update_file("data/global_memory.md", timing_msg, new_mem, mem_file.sha)
            except Exception as e:
                print(f"DEBUG: Failed to update {phase_key} timestamp: {e}")
        
        # Fetch bot repo for timed phases
        bot_repo_name = os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo')
        try:
            bot_repo = gh.get_repo(bot_repo_name)
            mem_file_obj = bot_repo.get_contents("data/global_memory.md")
            current_memory = mem_file_obj.decoded_content.decode('utf-8')
        except:
            current_memory = ""
        
        # === PHASE 0.6: AUTO-MERGE/CLOSE PRs (every 6 hours) ===
        if should_run_timed_phase(current_memory, 'LAST_PR_JUDGE', 6):
            print("DEBUG: Phase 0.6 — Judging open PRs (6h cycle)")
            try:
                all_repos = [r for r in repos_data.get('repositories', []) if not r.get('fork')]
                random.shuffle(all_repos)
                repos_for_judge = [gh.get_repo(r['full_name']) for r in all_repos[:2]]  # Only 2 repos to save tokens
                pr_action_taken = False
                for judge_repo in repos_for_judge:
                    try:
                        if pr_action_taken:
                            break
                        open_prs = list(judge_repo.get_pulls(state='open'))[:2]  # Max 2 PRs per repo
                        for pr in open_prs:
                            if pr.user.login == get_bot_login():
                                continue  # Skip our own PRs
                            
                            diff = requests.get(pr.diff_url).text[:5000]
                            judge_prompt = f"""You are Mayo, the Reviewer AI. Judge this PR for auto-merge or auto-close.

Repo: {judge_repo.full_name}
PR #{pr.number}: {pr.title}
Author: {pr.user.login}
Diff:
{diff}

Rate this PR's reasonableness (0-100) and decide: merge, close, or skip.
Output ONLY JSON: {{"reasonableness_score": 85, "action": "merge|close|skip", "reason": "explanation"}}"""
                            
                            response, _ = query_gemini_newcrons(judge_prompt)
                            if response:
                                verdict = extract_json_from_response(response)
                                if verdict:
                                    score = verdict.get('reasonableness_score', 50)
                                    action = verdict.get('action', 'skip')
                                    reason = verdict.get('reason', '')
                                    
                                    # DISABLED: Auto-merge triggers email to watchers
                                    # if action == 'merge' and score >= 85:
                                    #     try:
                                    #         pr.merge(merge_method='squash')
                                    #         pr.create_issue_comment(f"✅ Auto-merged by Mayo (score: {score}/100)\n\n{reason}")
                                    #         print(f"DEBUG: Auto-merged PR #{pr.number} on {judge_repo.name} (score: {score})")
                                    #     except Exception as e:
                                    #         print(f"DEBUG: Merge failed: {e}")
                                    # DISABLED: Auto-close triggers email to watchers
                                    # if action == 'close' and score <= 30:
                                    #     pr.edit(state='closed')
                                    #     pr.create_issue_comment(f"❌ Closed by Mayo — not reasonable (score: {score}/100)\n\n{reason}")
                                    #     print(f"DEBUG: Auto-closed PR #{pr.number} on {judge_repo.name} (score: {score})")
                                    #     pr_action_taken = True
                                    pass
                    except Exception as e:
                        print(f"DEBUG: Error judging PRs on {judge_repo.name}: {e}")
                
                update_phase_timestamp(bot_repo, current_memory, 'LAST_PR_JUDGE')
            except Exception as e:
                print(f"DEBUG: Phase 0.6 error: {e}")
        else:
            print("DEBUG: Phase 0.6 — Skipping PR judge (not yet 6h)")
        
        # === PHASE 0.7: AUTO-FIX/CLOSE ISSUES (every 6 hours) ===
        if should_run_timed_phase(current_memory, 'LAST_ISSUE_JUDGE', 6):
            print("DEBUG: Phase 0.7 — Judging open issues (6h cycle)")
            try:
                all_repos_i = [r for r in repos_data.get('repositories', []) if not r.get('fork')]
                random.shuffle(all_repos_i)
                repos_for_issues = [gh.get_repo(r['full_name']) for r in all_repos_i[:2]]  # Only 2 repos
                issue_action_taken = False
                for issue_repo in repos_for_issues:
                    try:
                        if issue_action_taken:
                            break
                        open_issues = [i for i in issue_repo.get_issues(state='open') if not i.pull_request][:2]  # Max 2 issues
                        for issue in open_issues:
                            judge_prompt = f"""You are Mayo, the Reviewer AI. Judge this issue.

Repo: {issue_repo.full_name}
Issue #{issue.number}: {issue.title}
Body: {(issue.body or '')[:3000]}
Labels: {[l.name for l in issue.labels]}

Rate reasonableness (0-100) and decide: fix (open a PR to solve it), close (unreasonable/spam/outdated), or skip.
Output ONLY JSON: {{"reasonableness_score": 70, "action": "fix|close|skip", "reason": "explanation", "fix_plan": "if action=fix, describe what to change"}}"""
                            
                            response, _ = query_gemini_newcrons(judge_prompt)
                            if response:
                                verdict = extract_json_from_response(response)
                                if verdict:
                                    score = verdict.get('reasonableness_score', 50)
                                    action = verdict.get('action', 'skip')
                                    reason = verdict.get('reason', '')
                                    
                                    # DISABLED: Auto-close triggers email to watchers
                                    # if action == 'close' and score <= 30:
                                    #     issue.edit(state='closed')
                                    #     issue.create_comment(f"❌ Closed by Mayo — not actionable (score: {score}/100)\n\n{reason}")
                                    #     print(f"DEBUG: Auto-closed issue #{issue.number} on {issue_repo.name}")
                                    # DISABLED: Issue comment triggers email to watchers
                                    # elif action == 'fix' and score >= 70:
                                    #     issue.create_comment(f"🔧 Mayo is working on a fix for this... (score: {score}/100)")
                                    #     print(f"DEBUG: Issue #{issue.number} on {issue_repo.name} queued for auto-fix (score: {score})")
                                    #     issue_action_taken = True
                                    pass
                    except Exception as e:
                        print(f"DEBUG: Error judging issues on {issue_repo.name}: {e}")
                
                update_phase_timestamp(bot_repo, current_memory, 'LAST_ISSUE_JUDGE')
            except Exception as e:
                print(f"DEBUG: Phase 0.7 error: {e}")
        else:
            print("DEBUG: Phase 0.7 — Skipping issue judge (not yet 6h)")
        
        # === PHASE I: PROACTIVE ISSUES (once per day) ===
        if should_run_timed_phase(current_memory, 'LAST_PROACTIVE_ISSUE', 24):
            print("DEBUG: Phase I — Proactive issue scan (24h cycle)")
            try:
                # ONLY git-pulse
                issue_candidates = [r for r in repos_data.get('repositories', [])
                                    if r.get('name') == 'git-pulse']
                
                if issue_candidates:
                    chosen_repo_data = random.choice(issue_candidates)
                    proactive_repo = gh.get_repo(chosen_repo_data['full_name'])
                    
                    # Quick scan
                    structure = get_repo_structure(proactive_repo, max_depth=2)
                    readme = read_file_content(proactive_repo, "README.md") or "(No README)"
                    
                    proactive_prompt = f"""You are the SCANNER. Analyze this repo for something that CANNOT be fixed with a simple PR.

Repo: {proactive_repo.full_name}
Structure:
{structure}
README:
{readme[:3000]}

Look for:
- Missing CI/CD setup
- Architectural concerns
- Feature ideas that need discussion
- Security audit recommendations
- Missing test coverage strategy

If you find something worth raising, output:
DIRECTIVE: PROACTIVE_ISSUE
TITLE: [short title]
BODY: [detailed explanation with recommendations]

If the repo looks fine, output: SKIP"""
                    
                    result, _ = query_gemini_newcrons(proactive_prompt)
                    if result and 'DIRECTIVE: PROACTIVE_ISSUE' in result:
                        import re as re_mod
                        title_match = re_mod.search(r'TITLE:\s*(.+)', result)
                        body_match = re_mod.search(r'BODY:\s*([\s\S]+?)(?:\n\n##|\Z)', result)
                        
                        issue_title = title_match.group(1).strip() if title_match else f"🤖 Improvement suggestion for {proactive_repo.name}"
                        issue_body = body_match.group(1).strip() if body_match else result
                        
                        new_issue = proactive_repo.create_issue(
                            title=f"🤖 {issue_title}",
                            body=f"{issue_body}\n\n---\n*Proactively opened by Mayo 🤖 — Daily Architecture Scan*",
                            labels=['mayo-generated']
                        )
                        print(f"DEBUG: Proactive issue created: {new_issue.html_url}")
                    else:
                        print("DEBUG: No proactive issue needed (repo looks clean)")
                
                update_phase_timestamp(bot_repo, current_memory, 'LAST_PROACTIVE_ISSUE')
            except Exception as e:
                print(f"DEBUG: Phase I error: {e}")
        else:
            print("DEBUG: Phase I — Skipping proactive issues (not yet 24h)")
        
        # === PHASE D: DISCUSSIONS AUTO-REPLY (every 6 hours) ===
        if should_run_timed_phase(current_memory, 'LAST_DISCUSSION_REPLY', 6):
            print("DEBUG: Phase D — Checking discussions (6h cycle)")
            try:
                def graphql_query(token_val, query, variables=None):
                    """Execute a GitHub GraphQL query."""
                    r = requests.post(
                        'https://api.github.com/graphql',
                        json={'query': query, 'variables': variables or {}},
                        headers={'Authorization': f'bearer {token_val}', 'Content-Type': 'application/json'},
                        timeout=30
                    )
                    r.raise_for_status()
                    return r.json()
                
                # Check each repo for unanswered discussions
                for repo_data in repos_data.get('repositories', [])[:3]:  # Only 3 repos to save tokens
                    owner, name = repo_data['full_name'].split('/')
                    try:
                        result = graphql_query(token, """
                            query($owner: String!, $name: String!) {
                                repository(owner: $owner, name: $name) {
                                    discussions(first: 5, orderBy: {field: CREATED_AT, direction: DESC}) {
                                        nodes {
                                            id
                                            number
                                            title
                                            body
                                            comments(first: 1) {
                                                totalCount
                                            }
                                        }
                                    }
                                }
                            }
                        """, {'owner': owner, 'name': name})
                        
                        discussions = result.get('data', {}).get('repository', {}).get('discussions', {}).get('nodes', [])
                        for disc in discussions:
                            if disc['comments']['totalCount'] == 0:
                                print(f"DEBUG: Found unanswered discussion #{disc['number']}: {disc['title']}")
                                
                                structure = get_repo_structure(gh.get_repo(repo_data['full_name']), max_depth=2)
                                disc_prompt = f"""You are Mayo, a helpful AI assistant for the repo {repo_data['full_name']}.
Someone posted a discussion titled: "{disc['title']}"
Body: {(disc['body'] or '')[:3000]}

Repo structure:
{structure}

Write a helpful, concise reply. Be friendly and technical. If it's a question, answer it. If it's a feature request, analyze feasibility. If it's a bug report, suggest next steps."""
                                
                                reply, _ = query_gemini_newcrons(disc_prompt)
                                if reply:
                                    # Post reply via GraphQL
                                    graphql_query(token, """
                                        mutation($discussionId: ID!, $body: String!) {
                                            addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
                                                comment { id }
                                            }
                                        }
                                    """, {'discussionId': disc['id'], 'body': f"{reply}\n\n---\n*Replied by Mayo 🤖*"})
                                    print(f"DEBUG: Replied to discussion #{disc['number']} on {repo_data['full_name']}")
                    except Exception as e:
                        if 'FORBIDDEN' not in str(e) and 'NOT_FOUND' not in str(e):
                            print(f"DEBUG: Discussion error on {repo_data['full_name']}: {e}")
                
                update_phase_timestamp(bot_repo, current_memory, 'LAST_DISCUSSION_REPLY')
            except Exception as e:
                print(f"DEBUG: Phase D error: {e}")
        else:
            print("DEBUG: Phase D — Skipping discussions (not yet 6h)")
        
        # Fetch Global Memory first for priority/cooldown analysis
        bot_repo_name = os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo')
        print(f"DEBUG: BOT_REPO_NAME = '{bot_repo_name}'")
        try:
            bot_repo = gh.get_repo(bot_repo_name)
            print(f"DEBUG: Successfully accessed bot repo: {bot_repo.full_name}")
            memory_file_obj = bot_repo.get_contents("data/global_memory.md")
            global_memory = memory_file_obj.decoded_content.decode('utf-8')
            print(f"DEBUG: Global memory fetched (len: {len(global_memory)})")
        except Exception as e:
            print(f"DEBUG: PyGithub failed to fetch global memory: {e}")
            mem_url = f"https://api.github.com/repos/{bot_repo_name}/contents/data/global_memory.md"
            mem_resp = requests.get(mem_url, headers=headers)
            print(f"DEBUG: REST API fallback status: {mem_resp.status_code}")
            if mem_resp.status_code == 200:
                import base64
                global_memory = base64.b64decode(mem_resp.json()['content']).decode('utf-8')
                print(f"DEBUG: Global memory fetched via REST (len: {len(global_memory)})")
            else:
                print(f"DEBUG: REST fallback also failed: {mem_resp.text[:200]}")
                global_memory = "No global memory found. Start with fresh excellence."

# === REPO SELECTION: ALL REPOS EXCEPT BLACKLISTED ===
        candidates = [r for r in repos_data.get('repositories', [])
                       if not r.get('fork') and r.get('name') not in EXCLUDED_REPOS]

        if not candidates:
            print("DEBUG: No eligible repos found (all excluded or no repos)")
            return

        chosen = random.choice(candidates)
        target_repo = gh.get_repo(chosen['full_name'])
        print(f"DEBUG: Targeting repo {target_repo.full_name} (random selection, {len(candidates)} eligible)")

        # Gather codebase context
        structure = get_repo_structure(target_repo, max_depth=3)
        readme_content = read_file_content(target_repo, "README.md") or "(No README)"
        
        # === DEEP RECURSIVE FILE SCANNING (v3) ===
        # ONE API call gets entire repo tree — no more shallow root + hardcoded dirs
        source_files = []
        EXCLUDED_FILES = ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lockb', '.min.js', '.min.css']
        CODE_EXTENSIONS = ['.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.c', '.cpp', '.h', '.rs', '.java', '.rb', '.php', '.swift', '.kt']
        DOC_EXTENSIONS = ['.md', '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.env.example']
        
        try:
            tree = target_repo.get_git_tree(target_repo.default_branch, recursive=True)
            for item in tree.tree:
                if item.type != 'blob':
                    continue
                if any(excl in item.path for excl in EXCLUDED_FILES):
                    continue
                if item.path.startswith('.') or '/.' in item.path:
                    continue
                if any(item.path.endswith(ext) for ext in CODE_EXTENSIONS + DOC_EXTENSIONS):
                    source_files.append(item.path)
            print(f"DEBUG: Recursive tree found {len(source_files)} eligible files")
        except Exception as e:
            print(f"DEBUG: get_git_tree failed ({e}), falling back to shallow scan")
            try:
                contents = target_repo.get_contents("")
                for item in contents:
                    if item.type == 'file' and any(item.name.endswith(ext) for ext in CODE_EXTENSIONS + DOC_EXTENSIONS):
                        if not any(excl in item.name for excl in EXCLUDED_FILES):
                            source_files.append(item.path)
            except Exception as e2:
                print(f"DEBUG: Shallow scan also failed: {e2}")
        
        if not source_files:
            if readme_content != "(No README found)":
                source_files = ["README.md"]
            else:
                print("No source files found")
                return
        
        # Smart file selection: prioritize CODE files (2) over docs/config (1) to fit in API payload limits
        code_files = [f for f in source_files if any(f.endswith(ext) for ext in CODE_EXTENSIONS)]
        doc_files = [f for f in source_files if any(f.endswith(ext) for ext in DOC_EXTENSIONS)]
        
        random.shuffle(code_files)
        random.shuffle(doc_files)
        
        # Limit to 1 code file + 1 doc to reduce prompt size (prevent Groq/Gemini failures)
        target_paths = code_files[:1] + doc_files[:1]
        if not target_paths:
            target_paths = source_files[:2]
        random.shuffle(target_paths)
        
        file_contents = ""
        for tp in target_paths:
            content = read_file_content(target_repo, tp)
            if content:
                # Truncate each file to 5000 chars (smaller for Groq/Gemini limits)
                if len(content) > 5000:
                    content = content[:5000] + "\n...[TRUNCATED FOR LENGTH]..."
                
                # CRITICAL: Remove image file references to prevent NVIDIA NIM from trying to load images
                import re as re_mod
                # Remove markdown image syntax: ![alt](image.png)
                content = re_mod.sub(r'!\[[^\]]*\]\([^\)]+\.(?:png|jpg|jpeg|gif|bmp|webp|svg)\)', '[IMAGE_REMOVED]', content, flags=re_mod.IGNORECASE)
                # Remove HTML img tags: <img src="image.png">
                content = re_mod.sub(r'<img[^>]*src=[\'"]([^\'"]*\.(?:png|jpg|jpeg|gif|bmp|webp|svg))[\'"][^>]*>', '[IMAGE_REMOVED]', content, flags=re_mod.IGNORECASE)
                # Remove CSS background-image: url(image.png)
                content = re_mod.sub(r'url\s*\(\s*[\'"]([^\'"]*\.(?:png|jpg|jpeg|gif|bmp|webp|svg))[\'"]\s*\)', 'url("[IMAGE_REMOVED]")', content, flags=re_mod.IGNORECASE)
                # Remove standalone image file references that might be mistaken as paths
                content = re_mod.sub(r'\b[\w/\\-]+\.(?:png|jpg|jpeg|gif|bmp|webp|svg)\b', '[IMAGE_FILE]', content, flags=re_mod.IGNORECASE)
                
                file_contents += f"\n--- {tp} ---\n{content}\n"
        
        if not file_contents:
            print("DEBUG: Could not read target files")
            return

        ts = int(time.time())
        target_path_display = ", ".join(target_paths)

        # === PHASE 1: SCANNER (Gemini A) ===
        print(f"DEBUG: Phase 1 — Scanner analyzing {target_path_display}")
        try:
            scanner_path = os.path.join(os.path.dirname(__file__), 'prompts', 'scanner_prompt.txt')
            with open(scanner_path, 'r') as f:
                scanner_template = f.read()
            
            # Escape curly braces for JSON
            scanner_prompt = scanner_template.replace('{{REPO_NAME}}', target_repo.full_name)\
                                             .replace('{{FILE_PATH}}', target_path_display)\
                                             .replace('{{REPO_STRUCTURE}}', structure)\
                                             .replace('{{README_CONTENT}}', readme_content)\
                                             .replace('{{FILE_CONTENT}}', file_contents)\
                                             .replace('{{GLOBAL_MEMORY}}', global_memory)
        except Exception as e:
            print(f"DEBUG: Failed to load scanner prompt: {e}")
            scanner_prompt = f"Analyze {target_repo.full_name} ({target_path_display}) and recommend one improvement. Text only, no code."
        
        scanner_plan, scanner_model = query_gemini_scanner(scanner_prompt)
        if not scanner_plan:
            print("DEBUG: Scanner returned nothing")
            return
        
        print(f"DEBUG: Scanner plan length: {len(scanner_plan)}")

        # === OPEN_ISSUE DIRECTIVE: Scanner wants human input ===
        if 'DIRECTIVE: OPEN_ISSUE' in scanner_plan:
            print("DEBUG: Scanner requested OPEN_ISSUE — opening GitHub Issue instead of PR")
            try:
                import re as re_mod
                title_match = re_mod.search(r'TITLE:\s*(.+)', scanner_plan)
                body_match = re_mod.search(r'BODY:\s*([\s\S]+?)(?:\n\n##|\Z)', scanner_plan)
                issue_title = title_match.group(1).strip() if title_match else f"🤖 Scanner needs input on {target_repo.name}"
                issue_body = body_match.group(1).strip() if body_match else scanner_plan
                
                owner_login = target_repo.owner.login
                full_body = (
                    f"The Scanner AI found something on **{target_repo.name}** that needs your input before I can proceed.\n\n"
                    f"---\n\n{issue_body}\n\n"
                    f"---\n\n"
                    f"**Please reply with your decision** and I'll pick it up on the next cycle.\n\n"
                    f"*Generated by Mayo 🤖 — Triple-AI Pipeline (Scanner flagged OPEN_ISSUE)*"
                )
                issue = target_repo.create_issue(title=f"🤖 {issue_title}", body=full_body)
                print(f"DEBUG: Issue created: {issue.html_url}")
                
                # Save to memory
                try:
                    bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
                    mem_file = bot_repo.get_contents("data/global_memory.md")
                    old_mem = mem_file.decoded_content.decode('utf-8')
                    note = f"\n- **Repo: {target_repo.name}**: Opened issue — {issue_title}. (Ref: {issue.html_url}) - *Status: AWAITING JOSEPH'S INPUT*"
                    bot_repo.update_file("data/global_memory.md", co_author_msg(f"feat(memory): scanner opened issue on {target_repo.name}"), old_mem + note, mem_file.sha)
                except Exception as e:
                    print(f"DEBUG: Failed to save issue to memory: {e}")
            except Exception as e:
                print(f"DEBUG: Failed to create issue: {e}")
            return

        # === PHASE 2 & 3: EXECUTOR + REVIEWER (with retry) ===
        max_attempts = 2
        reviewer_feedback = ""
        final_edits = None
        final_title = ""
        final_body = ""
        final_branch = ""
        reviewer_verdict_text = ""

        for attempt in range(1, max_attempts + 1):
            print(f"DEBUG: Phase 2 — Executor attempt {attempt}")
            
            # Load executor prompt
            try:
                executor_path = os.path.join(os.path.dirname(__file__), 'prompts', 'executor_prompt.txt')
                with open(executor_path, 'r') as f:
                    executor_template = f.read()
                
                executor_prompt = executor_template.replace('{{REPO_NAME}}', target_repo.full_name)\
                                                   .replace('{{FILE_PATH}}', target_path_display)\
                                                   .replace('{{REPO_STRUCTURE}}', structure)\
                                                   .replace('{{SCANNER_PLAN}}', scanner_plan)\
                                                   .replace('{{FILE_CONTENT}}', file_contents)\
                                                   .replace('{{GLOBAL_MEMORY}}', global_memory)\
                                                   .replace('{{TIMESTAMP}}', str(ts))\
                                                   .replace('{{REVIEWER_FEEDBACK}}', reviewer_feedback)
            except Exception as e:
                print(f"DEBUG: Failed to load executor prompt: {e}")
                break
            
            # --- THE DUAL EXECUTOR & FALLBACK ARCHITECTURE (Fireworks + Groq) ---
            print("DEBUG: Executing Fireworks Executor first...")
            executor1_resp, executor1_model = query_fireworks_executor(executor_prompt)
            
            data1 = extract_json_from_response(executor1_resp) if executor1_resp else None
            
            improvement_data = None
            used_model = executor1_model or "Fireworks AI (Llama 3.3 70B)"
            
            # If Fireworks succeeds, use it
            if data1 and 'edits' in data1:
                improvement_data = data1
            else:
                print("DEBUG: Fireworks failed. Trying Dual Groq Llama 3.3...")
                executor1_resp, executor1_model = query_groq(executor_prompt, api_key=os.environ.get('GROK_API_KEY'))
                executor2_resp, executor2_model = query_groq(executor_prompt, api_key=os.environ.get('GROK_2ND_EXECUTOR_API_KEY'))
                
                data1 = extract_json_from_response(executor1_resp) if executor1_resp else None
                data2 = extract_json_from_response(executor2_resp) if executor2_resp else None
                
                # Combine successful edits from EXECUTOR 1 and EXECUTOR 2
                if (data1 and 'edits' in data1) or (data2 and 'edits' in data2):
                    improvement_data = data1 if data1 else data2
                    used_model = executor1_model or executor2_model or "Groq (llama-3.1-8b-instant)"
                    if data1 and data2 and 'edits' in data1 and 'edits' in data2:
                        print("DEBUG: Both Executors succeeded! Combining their surgical edits...")
                        improvement_data['edits'].extend(data2.get('edits', []))
                else:
                    print("DEBUG: Both Groq Executors failed or returned invalid JSON. Checking fallbacks...")
                    # Fallback 1: The Groq Fallback Key
                    fb1_resp, _ = query_groq(executor_prompt, api_key=os.environ.get('GROK_FALLBACK_API_KEY'))
                    improvement_data = extract_json_from_response(fb1_resp) if fb1_resp else None
                    
                    if not improvement_data or 'edits' not in improvement_data:
                        # Fallback 2: The Ultimate Gemini Executor
                        print("DEBUG: Groq Fallback failed. Engaging Ultimate Gemini Executor...")
                        fb3_resp, fb3_model = query_gemini_executor(executor_prompt)
                        used_model = fb3_model or used_model
                        if fb3_resp:
                            improvement_data = extract_json_from_response(fb3_resp)
            
            if not improvement_data or 'edits' not in improvement_data:
                print("DEBUG: ALL Executors and Fallbacks failed. No valid JSON produced.")
                break
                
            print(f"DEBUG: Successfully acquired {len(improvement_data.get('edits', []))} total edits.")

            # === PHASE 3: REVIEWER (Gemini B) — validates ACTUAL DIFF ===
            print(f"DEBUG: Phase 3 — Reviewer validating attempt {attempt}")
            
            # Pre-apply edits to compute a real diff for the Reviewer
            proposed_edits = improvement_data.get('edits', [])
            diff_preview = ""
            import difflib
            diff_preview = ""
            for edit in proposed_edits:
                fpath = edit.get('file') or target_paths[0]
                original = read_file_content(target_repo, fpath) or ""
                patched = apply_surgical_edits(original, [edit])
                if patched != original:
                    orig_lines = original.splitlines()
                    new_lines = patched.splitlines()
                    diff = difflib.unified_diff(orig_lines, new_lines, fromfile='original', tofile='patched', lineterm='')
                    diff_preview += f"\n--- {fpath}\n" + '\n'.join(list(diff)[:50]) # Show first 50 lines of diff
                    if len(list(diff)) > 50: diff_preview += "\n...[diff truncated]..."
                else:
                    diff_preview += f"\n--- {fpath}: NO CHANGES (search block not found or blocked by safety guard)\n"
            
            try:
                reviewer_path = os.path.join(os.path.dirname(__file__), 'prompts', 'reviewer_prompt.txt')
                with open(reviewer_path, 'r') as f:
                    reviewer_template = f.read()
                
                reviewer_prompt = reviewer_template.replace('{{REPO_NAME}}', target_repo.full_name)\
                                                   .replace('{{FILE_PATH}}', target_path_display)\
                                                   .replace('{{FILE_CONTENT}}', file_contents)\
                                                   .replace('{{PROPOSED_EDITS}}', json.dumps(proposed_edits))\
                                                   .replace('{{SCANNER_PLAN}}', scanner_plan)\
                                                   .replace('{{GLOBAL_MEMORY}}', global_memory)
                # Append the actual diff so Reviewer sees real impact
                reviewer_prompt += f"\n\n## ACTUAL DIFF PREVIEW (what will be committed):\n{diff_preview}\n\nIMPORTANT: Read the diff carefully. If it looks correct and matches the intent, APPROVE."
            except Exception as e:
                print(f"DEBUG: Failed to load reviewer prompt: {e}")
                final_edits = improvement_data.get('edits', [])
                final_title = improvement_data.get('title', 'Automated improvement')
                final_body = improvement_data.get('body', '')
                final_branch = improvement_data.get('branch_name', f'bot/fix-{ts}')
                break
            
            reviewer_response, reviewer_model = query_gemini_reviewer(reviewer_prompt)
            if not reviewer_response:
                print("DEBUG: Reviewer returned nothing, using Executor's edits as-is")
                final_edits = improvement_data.get('edits', [])
                final_title = improvement_data.get('title', 'Automated improvement')
                final_body = improvement_data.get('body', '')
                final_branch = improvement_data.get('branch_name', f'bot/fix-{ts}')
                reviewer_verdict_text = "Reviewer unavailable — used Executor's edits directly"
                break
            
            reviewer_data = extract_json_from_response(reviewer_response)
            
            if not reviewer_data:
                print("DEBUG: Could not parse Reviewer JSON, using Executor's edits")
                final_edits = improvement_data.get('edits', [])
                final_title = improvement_data.get('title', 'Automated improvement')
                final_body = improvement_data.get('body', '')
                final_branch = improvement_data.get('branch_name', f'bot/fix-{ts}')
                reviewer_verdict_text = "Reviewer response unparseable"
                break
            
            verdict = reviewer_data.get('verdict', 'REJECT').upper()
            reviewer_verdict_text = f"{verdict}: {reviewer_data.get('reason', 'No reason given')}"
            print(f"DEBUG: Reviewer verdict: {verdict}")
            
            if verdict == 'APPROVE':
                final_edits = improvement_data.get('edits', [])
                final_title = improvement_data.get('title', 'Automated improvement')
                final_body = improvement_data.get('body', '')
                final_branch = improvement_data.get('branch_name', f'bot/fix-{ts}')
                break
            elif verdict == 'CORRECT':
                corrected = reviewer_data.get('corrected_edits', improvement_data.get('edits', []))
                
                # Verify the Reviewer's corrections actually work and pass safety guards
                valid_corrections = False
                for edit in corrected:
                    fpath = edit.get('file') or target_paths[0]
                    original = read_file_content(target_repo, fpath) or ""
                    if apply_surgical_edits(original, [edit]) != original:
                        valid_corrections = True
                        break
                
                if not valid_corrections:
                    print("DEBUG: Reviewer's corrected edits failed safety guards or found no matches. Aborting.")
                    final_edits = []  # Clear edits so no PR is made
                    reviewer_verdict_text = "CORRECT (but corrected edits failed safety checks)"
                    break
                
                final_edits = corrected
                final_title = improvement_data.get('title', 'Automated improvement')
                final_body = improvement_data.get('body', '')
                final_branch = improvement_data.get('branch_name', f'bot/fix-{ts}')
                break
            elif verdict == 'REJECT':
                reviewer_feedback = reviewer_data.get('feedback_for_executor', 'Your edits were rejected. Try smaller, safer changes.')
                # Save rejection to memory
                memory_note = reviewer_data.get('memory_note', f'Rejected edit on {target_repo.name}')
                try:
                    bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
                    mem_file = bot_repo.get_contents("data/global_memory.md")
                    mem_content = mem_file.decoded_content.decode('utf-8')
                    mem_content += f"\n- **REJECTED by Reviewer**: {memory_note}"
                    bot_repo.update_file("data/global_memory.md", co_author_msg(f"feat(memory): reviewer rejected edit on {target_repo.name}"), mem_content, mem_file.sha)
                except Exception as e:
                    print(f"DEBUG: Failed to save rejection to memory: {e}")
                
                if attempt == max_attempts:
                    print("DEBUG: Max attempts reached, skipping PR")
                    # Log the failed cycle
                    executor_json_str = json.dumps(improvement_data) if improvement_data else "{}"
                    update_ai_communication_log(gh, ts, scanner_plan or "", executor_json_str, f"REJECTED x{max_attempts}: {reviewer_feedback}")
                    return
                continue

        if not final_edits:
            print("DEBUG: No valid edits after pipeline")
            return

        # Group edits by file
        file_edits = {}
        for edit in final_edits:
            # Fallback to the first target path if the AI forgot the file field
            fpath = edit.get('file') or target_paths[0]
            if fpath not in file_edits:
                file_edits[fpath] = []
            file_edits[fpath].append(edit)
            
        file_changes = {}
        for fpath, edits in file_edits.items():
            content = read_file_content(target_repo, fpath)
            if not content:
                print(f"DEBUG: Could not read {fpath} for edits")
                continue
            new_content = apply_surgical_edits(content, edits)
            if new_content != content:
                file_changes[fpath] = new_content
        
        if not file_changes:
            print("DEBUG: No changes applied after surgical edits (search failed or no valid files)")
            return

        # Create PR
        print(f"DEBUG: Creating branch {final_branch} with validated edits for {list(file_changes.keys())}")
        success, err = commit_changes_via_api(target_repo, final_branch, file_changes, final_title)
        
        if success:
            owner_login = target_repo.owner.login
            co_author_name = os.environ.get('CO_AUTHOR_NAME', '')
            co_author_email = os.environ.get('CO_AUTHOR_EMAIL', '')
            co_author_line = f"\n\nCo-authored-by: {co_author_name} <{co_author_email}>" if co_author_name and co_author_email else ""
            
            scanner_display = f"Scanner ({scanner_model})" if scanner_model else "Scanner"
            executor_display = f"Executor ({used_model})" if used_model else "Executor (Groq Llama)"
            reviewer_display = f"Reviewer ({reviewer_model})" if reviewer_model else "Reviewer (Gemini)"
            
            pr = target_repo.create_pull(
                title=f"[DRAFT] {final_title}",
                body=f"{final_body}\n\n---\n*Validated by Triple-AI: {scanner_display} → {executor_display} → {reviewer_display}*\n\nGenerated autonomously by Mayo 🤖{co_author_line}\n\n**This is a DRAFT PR — review and merge when ready.**",
                head=final_branch,
                base=target_repo.default_branch,
                draft=True
            )
            
            print(f"DEBUG: PR created: {pr.html_url}")
            
            # Save to memory
            try:
                bot_repo = gh.get_repo(os.environ.get('BOT_REPO_NAME', 'HOLYKEYZ/mayo'))
                old_memory_file = bot_repo.get_contents("data/global_memory.md")
                old_memory = old_memory_file.decoded_content.decode('utf-8')
                lesson = f"\n- **Repo: {target_repo.name}**: {final_title}. (Ref: {pr.html_url}) - *Status: PENDING REVIEW*"
                bot_repo.update_file(
                    "data/global_memory.md",
                    co_author_msg(f"feat(memory): record lesson from {target_repo.name}"),
                    old_memory + lesson,
                    old_memory_file.sha
                )
            except Exception as e:
                print(f"DEBUG: Failed to update memory: {e}")
            
            # Log the successful cycle
            update_ai_communication_log(gh, ts, scanner_plan or "", json.dumps(improvement_data) if improvement_data else "", reviewer_verdict_text)

            print("SUCCESS")
        else:
            print("DEBUG: Commit failed")
        
    except Exception as e:
        print(f"Cron error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_cron()
