import re
import json
import ast

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

def extract_json_from_response(text):
    if not text: return None
    json_patterns = [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```', r'\{[\s\S]*"edits"[\s\S]*\}']
    
    for pattern in json_patterns:
        match = re.search(pattern, text)
        if match:
            extracted_text = match.group(1) if '```' in pattern else match.group(0)
            try:
                return json.loads(extracted_text)
            except Exception as e:
                # Attempt to fix the string before failing completely
                try:
                    parsed = ast.literal_eval(extracted_text.strip())
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
                
                # Fix unescaped newlines in strings
                try:
                    fixed_str = escape_newlines_in_json_strings(extracted_text)
                    return json.loads(fixed_str)
                except Exception as final_e:
                    print(f"Final parse error: {final_e}")
                    pass
                continue
    return None

test_text = """Here are the requested surgical edits for your file:

```json
{
  "title": "[CI] Fix backend workflow",
  "body": "Replaced missing run command",
  "edits": [
    {
      "file": ".github/workflows/backend_ci.yml",
      "search": "",
      "replace": "name: Backend CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: npm test"
    }
  ]
}
```
"""

result = extract_json_from_response(test_text)
if result:
    print("\nSUCCESS! Parsed JSON:")
    print(json.dumps(result, indent=2))
else:
    print("\nFAILED TO PARSE JSON.")
