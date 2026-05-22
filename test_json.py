import json, re

def extract_json_from_response(text):
    """Extract and parse JSON from AI response text.

    Tries multiple regex patterns to find a JSON object containing "edits".
    Returns the parsed dict or None if no valid JSON found.
    """
    if not text: return None
    json_patterns = [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```', r'\{[\s\S]*?"edits"[\s\S]*?\}']
    
    for pattern in json_patterns:
        match = re.search(pattern, text)
        if match:
            print('Matched pattern:', pattern[:15], '...')
            try:
                json_str = match.group(1) if '```' in pattern else match.group(0)
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                print('JSON parse error:', e)
                lines = json_str.split('\n')
                line_num = e.lineno
                print(f"Error around line {line_num}:")
                if line_num > 1:
                    print(lines[line_num-2])
                print(lines[line_num-1], "   <--- ERROR HERE")
                if line_num < len(lines):
                    print(lines[line_num])
                continue
            except Exception as e:
                print('JSON parse error:', e)
                continue
    return None

if __name__ == '__main__':
    with open('api/ai_communication.md', 'r', encoding='utf-8') as f:
        log_content = f.read()

    # Find ALL Executor JSON blocks
    blocks = re.findall(r'\*\*Executor\*\*: ```json\n(.*?)\n```', log_content, re.DOTALL)
    if blocks:
        for index, block in enumerate(blocks):
            current_block = '```json\n{' + block.split('{', 1)[-1] if '{' in block else block + '\n```'
            print(f"Testing parser on block {index + 1}/{len(blocks)} (length: {len(current_block)})...")
            result = extract_json_from_response(current_block)
            if result:
                print("Success! Keys:", result.keys())
            else:
                print("Failed completely")
    else:
        print("No blocks found")
