import os
import json
import time
import requests
import subprocess
import re
# When hunks are very large, truncate before sending to the LLM to avoid exceeding token limits.
MAX_HUNK_LINES = 200  # maximum lines of diff context per hunk
TRUNCATION_NOTICE = "\n... (truncated) ...\n"
from pathlib import Path
from typing import AsyncIterator, Optional, List, Dict, Any, Tuple

def split_into_hunks(patch: str) -> List[Tuple[int, str]]:
    """
    Parse a unified diff patch into discrete hunks, returning list of
    (new_start_line, hunk_text). Only hunks with both removals and additions
    are returned.
    """
    hunks: List[Tuple[int, str]] = []
    lines = patch.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        header = lines[i]
        if header.startswith('@@'):
            # Parse new file starting line from hunk header of form "@@ -a,b +c,d @@"
            m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', header)
            print("m: ", m)
            if m:
                new_start = int(m.group(1))
                # Collect hunk body
                hunk_lines = [header]
                i += 1
                while i < len(lines) and not lines[i].startswith('@@'):
                    hunk_lines.append(lines[i])
                    i += 1
                # Only include if both deletions and additions exist
                # if any(l.startswith('-') for l in hunk_lines) and any(l.startswith('+') for l in hunk_lines):
                hunks.append((new_start, ''.join(hunk_lines)))
                continue
        i += 1
    return hunks
   
def extract_added_line_numbers(hunk_text: str, new_start: int) -> List[int]:
    """
    Given a unified diff hunk (including header), return the list of line numbers
    in the new file corresponding to each added ('+') line.
    """
    lines = hunk_text.splitlines()
    # First line is the header, skip it
    current_new = new_start
    added_lines: List[int] = []
    for line in lines[1:]:
        if line.startswith(' '):
            # Context line: advances both old and new
            current_new += 1
        elif line.startswith('-'):
            # Removal: advances old only
            continue
        elif line.startswith('+'):
            # Addition: record current new line then advance
            added_lines.append(current_new)
            current_new += 1
        else:
            # Other (e.g. \ No newline at end), ignore
            continue
    return added_lines

from common import (
    base_image,
    output_vol,
    app,
    VOL_MOUNT_PATH,
)

from fintuning import finetune
from inference import Inference
from github_pr_scraper import scrape

import modal

# GitHub API Integration
@app.function(
    image=base_image,
    secrets=[modal.Secret.from_name("github-secret")],
)
def post_github_comment(repo_owner: str, repo_name: str, pr_number: int, comment: str, path: str, position: int) -> bool:
    """Post a comment to a GitHub PR.
    
    Args:
        repo_owner: Owner of the repository
        repo_name: Name of the repository
        pr_number: PR number to comment on
        comment: Comment text
        path: Path to the file being commented on
        position: Position in the diff to comment on
        
    Returns:
        True if comment was posted successfully
    """
    token = os.environ["GITHUB_TOKEN"]
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

# API Endpoint
web_image = base_image.pip_install("fastapi", "uvicorn", "cryptography.fernet")

@app.function(
    image=web_image,
    volumes={VOL_MOUNT_PATH: output_vol},
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Body, Request, Header
    from pydantic import BaseModel
    
    app = FastAPI(title="GitHub Code Review Bot")
    
    class ScrapeRequest(BaseModel):
        username: str
        repo_owner: str
        repo_name: str
    
    class TrainRequest(BaseModel):
        username: str
        repo_owner: str
    
    class ReviewRequest(BaseModel):
        username: str
        repo_owner: str
        code_content: str
        file_path: str
    
    class CommentRequest(BaseModel):
        repo_owner: str
        repo_name: str
        pr_number: int
        comment: str
        file_path: str
        position: int  # Line number in the diff
    
    @app.post("/scrape")
    async def scrape_endpoint(req: ScrapeRequest):
        """Scrape GitHub comments for a user"""
        try:
            count = scrape.remote(req.username, req.repo_owner, req.repo_name)
            return {"status": "success", "samples": count}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/train")
    async def train_endpoint(req: TrainRequest):
        """Train a model on scraped data"""
        try:
            finetune.remote(req.username, req.repo_owner)
            return {"status": "success", "message": f"Model trained for {req.username}"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/review")
    async def review_endpoint(req: ReviewRequest):
        """Generate a code review comment"""
        try:
            model = Inference()
            
            # Collect full comment
            comment = ""
            async for chunk in model.generate.remote_gen(
                req.code_content, 
                req.file_path, 
                req.username, 
                req.repo_owner
            ):
                comment += chunk
            
            return {"status": "success", "comment": comment}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/post-comment")
    async def post_comment_endpoint(req: CommentRequest):
        """Post a comment to a GitHub PR"""
        success = post_github_comment.remote(
            req.repo_owner,
            req.repo_name,
            req.pr_number,
            req.comment,
            req.file_path,
            req.position
        )
        
        if success:
            return {"status": "success", "message": "Comment posted"}
        else:
            raise HTTPException(status_code=500, detail="Failed to post comment")
    
    @app.post("/webhook")
    async def github_webhook(request: Request):
        """GitHub webhook: on '@tech-lead-bot' mention, scrape, train, review, and comment on the PR."""
        payload = await request.json()

        event = request.headers.get("X-GitHub-Event")
        # Only handle new PR comment events
        if event != "issue_comment" or payload.get("action") != "created":
            return {"status": "ignored", "reason": "not a new PR comment"}
        issue = payload.get("issue", {})
        if "pull_request" not in issue:
            return {"status": "ignored", "reason": "not a PR comment"}
        comment = payload.get("comment", {})
        body = comment.get("body", "")
        if "@tech-lead-bot" not in body:
            return {"status": "ignored", "reason": "bot not mentioned"}
        
        requested_user = body.split("@tech-lead-bot")[1].strip()
        if requested_user == "":
            requested_user = commenter
        

        commenter = comment.get("user", {}).get("login")
        repo_owner = payload.get("repository", {}).get("owner", {}).get("login")
        repo_name = payload.get("repository", {}).get("name")
        pr_number = issue.get("number")

        print("Commenter:", commenter,  "Repo Owner:", repo_owner, "Repo Name:", repo_name, "PR Number:", pr_number, "Requested User:", requested_user)
       
       # Schedule scraping and training
        # scrape.remote(requested_user, repo_owner, repo_name)
        # finetune.remote(requested_user, repo_owner)
        print("finished finetuning")

        webhook_functionality.remote(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pr_number=pr_number,
            commenter=commenter,
            requested_user=requested_user
        )
    

    from fastapi.responses import RedirectResponse
    from cryptography.fernet import Fernet
    import urllib.parse

    # Load client info
    client_id = os.environ["GITHUB_CLIENT_ID"]
    client_secret = os.environ["GITHUB_CLIENT_SECRET"]

    # This is where encrypted tokens are saved
    token_dir = Path(VOL_MOUNT_PATH) / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    fernet_key = Fernet.generate_key()  # You can persist this to a Modal Secret
    fernet = Fernet(fernet_key)

    @app.get("/auth/github/login")
    def github_login():
        """Redirect user to GitHub OAuth consent screen"""
        params = {
            "client_id": client_id,
            "scope": "repo",  # change if needed
            "redirect_uri": "https://riassharma10--github-codereview-bot-api.modal.run/auth/github/callback"
        }
        url = f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
        return RedirectResponse(url)

    @app.get("/auth/github/callback")
    def github_callback(code: str):
        """Receive GitHub code and exchange for access token"""
        # Exchange code for token
        resp = requests.post("https://github.com/login/oauth/access_token", headers={
            "Accept": "application/json"
        }, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code
        })

        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Token exchange failed")
        
        access_token = resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token returned")

        # Get username for storage
        user_resp = requests.get("https://api.github.com/user", headers={
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json"
        })
        user_resp.raise_for_status()
        username = user_resp.json()["login"]

        # Encrypt + store
        encrypted = fernet.encrypt(access_token.encode("utf-8"))
        with open(token_dir / f"{username}.token", "wb") as f:
            f.write(encrypted)

        return {"status": "success", "username": username}

    return app

# End-to-end function for quick usage
@app.function(
    image=base_image,
    volumes={VOL_MOUNT_PATH: output_vol},
    secrets=[modal.Secret.from_name("github-secret")],
)
def review_and_comment(
    username: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    file_path: str
):
    """Review code and post a comment in one go."""
    # Get PR file content
    token = os.environ["GITHUB_TOKEN"]
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
        for chunk in model.generate.remote_gen(hunk_text, file_path, username, repo_owner):
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
        success = post_github_comment.remote(
            repo_owner,
            repo_name,
            pr_number,
            comment,
            file_path,
            position
        )
        if success:
            print(f"Posted comment at line {position} in {file_path}")
        else:
            print(f"Failed to post comment at line {position}")


@app.function(
    image=base_image,
    volumes={VOL_MOUNT_PATH: output_vol},
    secrets=[modal.Secret.from_name("github-secret")],
)
def webhook_functionality(
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    commenter: str,
    requested_user: str,
):
    """Review code and post a comment in one go."""
    # Get PR file content
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    files_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/files"

    print("Fetching PR files from:", files_url)
    resp = requests.get(files_url, headers=headers)

    if resp.status_code != 200:
        raise Exception(f"Failed to fetch PR files: {resp.status_code} - {resp.text}")
    files = resp.json()
    # Schedule review and comments for each file
    for f in files:
        file_path = f.get("filename")
        print("writing a comment on this file: ", file_path)
        review_and_comment.remote(requested_user, repo_owner, repo_name, pr_number, file_path)
    return {"status": "scheduled"}
    