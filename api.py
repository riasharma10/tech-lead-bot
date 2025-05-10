import os
import json
import time
import requests
import subprocess
import re
# When hunks are very large, truncate before sending to the LLM to avoid exceeding token limits.
from pathlib import Path
from typing import AsyncIterator, Optional, List, Dict, Any, Tuple
from token_db import store_token
from token_db import load_token
from fastapi.responses import HTMLResponse
from github_actions import review_and_comment, post_github_comment


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


# API Endpoint
web_image = base_image.pip_install("fastapi", "uvicorn", "cryptography.fernet")

class WebhookContext:
    def __init__(self, payload: dict):
        self.comment = payload.get("comment", {})
        self.issue = payload.get("issue", {})
        self.repository = payload.get("repository", {})
        
        self.commenter = self.comment.get("user", {}).get("login")
        self.body = self.comment.get("body", "")
        self.repo_owner = self.repository.get("owner", {}).get("login")
        self.repo_name = self.repository.get("name")
        self.pr_number = self.issue.get("number")
        
        # Extract requested user from comment
        requested_user = self.body.split(" ")[1].strip()
        self.requested_user = requested_user if requested_user else self.commenter

        force_reload = self.body.split(" ")[2].strip()
        self.force_reload = force_reload == "--force-reload"

@app.function(
    image=web_image,
    volumes={VOL_MOUNT_PATH: output_vol},
    secrets=[modal.Secret.from_name("encryption-key"), modal.Secret.from_name("github-oauth"), modal.Secret.from_name("github-secret")],
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from pydantic import BaseModel
    from cryptography.fernet import Fernet
    import urllib.parse

    app = FastAPI(title="GitHub Code Review Bot")
    
    # Load client info
    client_id = os.environ["GITHUB_CLIENT_ID"]
    client_secret = os.environ["GITHUB_CLIENT_SECRET"]
    fernet = Fernet(os.environ["ENCRYPTION_KEY"])


    async def authenticate_user(context: WebhookContext) -> tuple[bool, str]:
        """Authenticate user and return (is_authenticated, token_or_auth_url)"""
        token = load_token(context.commenter)
        print("token: ", token)

        if token:
            # Verify token is still valid
            try:
                print("Loaded token. verifying.")
                response = requests.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github.v3+json"
                    }
                )
                if response.status_code == 200:
                    return True, token
            except:
                pass

        # Create OAuth URL with state
        state = fernet.encrypt(
            f"{context.commenter}:{context.repo_owner}:{context.repo_name}:{context.pr_number}".encode()
        ).decode()
        
        print("state: ", state)

        print("client_id: ", client_id)
        print("scope: ", "repo")
        print("state: ", state)
        print("redirect_uri: ", "https://riassharma10--github-codereview-bot-api.modal.run/auth/github/callback")
        params = {
            "client_id": client_id,
            "scope": "repo",
            "state": state,
            "redirect_uri": "https://riassharma10--github-codereview-bot-api.modal.run/auth/github/callback"
        }
        url = f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
        return False, url

    @app.post("/webhook")
    async def github_webhook(request: Request):
        """Handle GitHub webhook for PR comments"""
        # Validate webhook
        payload = await request.json()
        event = request.headers.get("X-GitHub-Event")
        
        if event != "issue_comment" or payload.get("action") != "created":
            return {"status": "ignored", "reason": "not a new PR comment"}
            
        if "pull_request" not in payload.get("issue", {}):
            return {"status": "ignored", "reason": "not a PR comment"}
            
        if "@tech-lead-bot" not in payload.get("comment", {}).get("body", ""):
            return {"status": "ignored", "reason": "bot not mentioned"}

        # Create context
        context = WebhookContext(payload)
        print(f"Processing webhook for {context.commenter} on PR #{context.pr_number}")

        # Authenticate user
        is_authenticated, auth_result = await authenticate_user(context)
        
        print("is_authenticated: ", is_authenticated)
        print("auth_result: ", auth_result)

        if not is_authenticated:
            # Return HTML that will open the auth URL in a new tab
            # 
            # return RedirectResponse(url=auth_result)
            bot_token = os.environ["GITHUB_TOKEN"]  # This comes from github-secret Modal secret
            headers = {
                "Authorization": f"token {bot_token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            comment_url = f"https://api.github.com/repos/{context.repo_owner}/{context.repo_name}/issues/{context.pr_number}/comments"
            comment_data = {
                "body": f"@{context.commenter} Please authorize the bot by clicking this link: {auth_result}"
            }
            
            response = requests.post(comment_url, headers=headers, json=comment_data)
            if response.status_code != 201:
                print(f"Failed to post comment: {response.status_code} - {response.text}")
                
            return {"status": "authentication_required", "message": "User needs to authenticate", "auth_url": auth_result}
 
        # User is authenticated, proceed with bot functionality
        try:
            # Scrape comments
            count = scrape.remote(
                username=context.requested_user, 
                repo_owner=context.repo_owner, 
                repo_name=context.repo_name, 
                force_reload=context.force_reload, 
                pr_number=context.pr_number, 
                commenter=context.commenter, 
                token=auth_result)
            
            print(f"Scraped {count} comments for {context.requested_user}")

            # Fine-tune model
            finetune.remote(
                username=context.requested_user,
                repo_owner= context.repo_owner, 
                repo_name=context.repo_name, 
                force_reload=context.force_reload)
            
            print("Finished fine-tuning")

            # Process webhook functionality
            webhook_functionality(
                repo_owner=context.repo_owner,
                repo_name=context.repo_name,
                pr_number=context.pr_number,
                commenter=context.commenter,
                requested_user=context.requested_user
            )

            return {"status": "success", "samples": count}
        except Exception as e:
            token = load_token(context.commenter) # This comes from github-secret Modal secret
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }

            comment_url = f"https://api.github.com/repos/{context.repo_owner}/{context.repo_name}/issues/{context.pr_number}/comments"
            comment_data = {
                "body": "Something went wrong. Please try again."
            }
            
            response = requests.post(comment_url, headers=headers, json=comment_data)
            if response.status_code != 201:
                print(f"Failed to post error notification: {response.status_code} - {response.text}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/auth/github/callback")
    @app.post("/auth/github/callback")
    async def github_callback(request: Request, code: str = None, state: str = None):
        """Handle GitHub OAuth callback"""
        print("=== CALLBACK ENDPOINT HIT ===")
        print(f"Method: {request.method}")
        print(f"Full URL: {request.url}")
        print(f"Query params: {request.query_params}")
        print(f"Code: {code}")
        print(f"State: {state}")
        try:
            # Decode state to get original context
            state_data = fernet.decrypt(state.encode()).decode()
            commenter, repo_owner, repo_name, pr_number = state_data.split(":")
            
            # Exchange code for token
            resp = requests.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code
                }
            )
            
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Token exchange failed")
            
            access_token = resp.json().get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="No access token returned")

            # Store the token
            store_token(commenter, access_token)
            print("storing token")  
            return {"status": "success", "message": "User authenticated"}

            # await RedirectResponse(url=f"https://github.com/{repo_owner}/{repo_name}/pull/{pr_number}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/test")
    async def test():
        return {"message": "Test endpoint working"}
    
    return app



def webhook_functionality(
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    commenter: str,
    requested_user: str,
):
    """Review code and post a comment in one go."""
    # Get PR file content
    token = load_token(commenter)
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
        review_and_comment(requested_user, repo_owner, repo_name, pr_number, file_path, commenter)
    return {"status": "scheduled"}
    