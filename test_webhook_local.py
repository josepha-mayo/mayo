import os
import sys

# Ensure API keys are loaded
if "GEMINI_API_KEY" not in os.environ:
    print("WARNING: GEMINI_API_KEY not set")

if "GITHUB_TOKEN" not in os.environ:
    print("WARNING: GITHUB_TOKEN not set")

# 1. Modify the mock event to match the user's screenshot
payload = {
    "action": "created",
    "repository": {
        "full_name": "HOLYKEYZ/joe-gemini"
    },
    "issue": {
        "number": 2,
        "title": "Missing CI/CD Strategy for Monorepo #2"
    },
    "comment": {
        "id": 123456789,
        "body": "mayo, try this time, it should work...",
        "user": {
            "login": "HOLYKEYZ",
            "type": "User"
        }
    },
    "installation": {
        "id": 1234  # Fake ID, will patch get_github_client
    }
}

import api.index as bot

from unittest.mock import MagicMock

# Create a full mock of the Github client
mock_gh = MagicMock()
mock_repo = MagicMock()
mock_gh.get_repo.return_value = mock_repo

mock_issue = MagicMock()
mock_issue.title = "Test Issue"
mock_issue.body = "Test body"
mock_issue.pull_request = False
mock_repo.get_issue.return_value = mock_issue

# Mock the comments list to simulate the user's comment
mock_prev_comment = MagicMock()
mock_prev_comment.id = 111
mock_prev_comment.user.login = "joe-gemini-bot[bot]"
mock_prev_comment.body = "Previous bot comment here"

mock_current_comment = MagicMock()
mock_current_comment.id = 123456789
mock_current_comment.user.login = "HOLYKEYZ"
mock_current_comment.body = "mayo, try this time, it should work..."

mock_issue.get_comments.return_value = [mock_prev_comment, mock_current_comment]

# Mock fetch_memory
bot.fetch_memory = MagicMock(return_value={"files_read": []})
bot.get_repo_structure = MagicMock(return_value="fake structure")
bot.get_context_expansion_files = MagicMock(return_value=[])
bot.get_bot_login = MagicMock(return_value="joe-gemini-bot[bot]")

def fake_get_github_client(install_id):
    return mock_gh

bot.get_github_client = fake_get_github_client

# Patch index.py prints to easily see where we are
original_handle = bot.handle_issue_comment

def trace_calls(frame, event, arg):
    if frame.f_code.co_name == 'handle_issue_comment':
        if event == 'line':
            print(f"Executing line {frame.f_lineno} in handle_issue_comment")
        elif event == 'return':
            print(f"<-- Returning from handle_issue_comment")
    return trace_calls

try:
    print("Testing handle_issue_comment locally with Mocks...")
    sys.settrace(trace_calls)
    original_handle(payload)
    sys.settrace(None)
    mock_issue.create_comment.assert_called()
    bot.fetch_memory.assert_called()
    bot.get_repo_structure.assert_called()
    print("Finished successfully")
except Exception as e:
    import traceback
    print("CRASHED:")
    traceback.print_exc()

