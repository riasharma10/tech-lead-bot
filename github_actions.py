from token_db import get_github_token
import requests
from parsing_helpers import split_into_hunks, extract_added_line_numbers
from inference import Inference
from token_db import load_token

MAX_HUNK_LINES = 200  # maximum lines of diff context per hunk
TRUNCATION_NOTICE = "\n... (truncated) ...\n"


def post_github_comment(repo_owner: str, repo_name: str, pr_number: int, comment: str, path: str, position: int, commenter: str, token: str) -> bool:
    """Post a comment to a GitHub PR.
    
    Args:
        repo_owner: Owner of the repository
        repo_name: Name of the repository
        pr_number: PR number to comment on
        comment: Comment text
        path: Path to the file being commented on
        position: Position in the diff to comment on
        commenter: Commenter's username

    Returns:
        True if comment was posted successfully
    """
    print("getting token in post_github_comment")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/comments"
    
    data = {
        "body": comment,
        "commit_id": None,  # Will be filled in by GitHub from the PR
        "path": path,
        "line": position
    }
    
    # Get the latest commit in the PR to use as commit_id
    commit_response = requests.get(
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/commits",
        headers=headers
    )
    
    if commit_response.status_code == 200:
        commits = commit_response.json()
        if commits:
            data["commit_id"] = commits[-1]["sha"]
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code in (201, 200):
        print(f"Comment posted successfully to PR #{pr_number}")
        return True
    else:
        print(f"Failed to post comment: {response.status_code} - {response.text}")
        return False

def review_and_comment(
    username: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    file_path: str,
    commenter: str,
    token: str
):
    """Review code and post a comment in one go."""
    # Get PR file content
    # token = load_token(commenter)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    # Get the file content from the PR
    file_resp = requests.get(
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/files",
        headers=headers
    )
    
    if file_resp.status_code != 200:
        print(f"Error getting PR files: {file_resp.status_code}")
        return
    
    files = file_resp.json()
    target_file = None

    for file in files:
        if file["filename"] == file_path:
            target_file = file
            break
    
    if not target_file:
        print(f"File {file_path} not found in PR")
        return
    
    # Split the diff into individual hunks
    patch = target_file.get("patch", "")
    hunks = split_into_hunks(patch)

    print(f"Split into {len(hunks)} hunk(s) in {file_path}")
    
    if not hunks:
        # fallback to a single hunk starting at line 1
        hunks = [(1, patch)]

    model = Inference()
    # For each hunk, generate and post a comment
    for new_start, hunk_text in hunks:
        # Truncate overly long hunks to avoid exceeding model context length
        lines = hunk_text.splitlines(keepends=True)
        if len(lines) > MAX_HUNK_LINES:
            head = lines[:MAX_HUNK_LINES//2]
            tail = lines[-MAX_HUNK_LINES//2:]
            hunk_text = ''.join(head) + TRUNCATION_NOTICE + ''.join(tail)
        # Generate comment for this hunk
        comment = ""
        for chunk in model.generate.remote_gen(hunk_text, file_path, username, repo_owner, repo_name):
            comment += chunk
        if not comment.strip():
            continue

        # Map '+' lines to actual file positions
        positions = extract_added_line_numbers(hunk_text, new_start)
        print("New start", new_start)
        print("Positions", positions)

        if not positions:
            # fallback to header start
            positions = [new_start]
        # Post one comment at the first added line
        position = positions[0]
        success = post_github_comment(
            repo_owner,
            repo_name,
            pr_number,
            comment,
            file_path,
            position,
            commenter,
            token
        )
        if success:
            print(f"Posted comment at line {position} in {file_path}")
        else:
            print(f"Failed to post comment at line {position}")


def write_status_comment(repo_owner: str, repo_name: str, pr_number: int, comment_body: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    comment_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pr_number}/comments"
    comment_data = {
        "body": comment_body
    }
    response = requests.post(comment_url, headers=headers, json=comment_data)
    if response.status_code != 201:
        print(f"Failed to post comment: {response.status_code} - {response.text}")