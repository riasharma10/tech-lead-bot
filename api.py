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
from github_actions import review_and_comment, post_github_comment, write_status_comment
from github_pr_scraper import get_user_model_path


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
web_image = base_image.pip_install("fastapi", "uvicorn", "cryptography.fernet", "requests", "PyJWT")

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

        if "--force-reload" in self.body:
            self.force_reload = True
        else:
            self.force_reload = False


processed_deliveries = set()
        

@app.function(
    image=web_image,
    volumes={VOL_MOUNT_PATH: output_vol},
    secrets=[modal.Secret.from_name("encryption-key"), modal.Secret.from_name("github-oauth"), modal.Secret.from_name("github_app_private_key"), modal.Secret.from_name("app-id")],
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from pydantic import BaseModel
    from cryptography.fernet import Fernet
    import urllib.parse
    import base64
    from jwt import encode

    app = FastAPI(title="GitHub Code Review Bot")
    
    # Load client info
    client_id = os.environ["GITHUB_CLIENT_ID"]
    client_secret = os.environ["GITHUB_CLIENT_SECRET"]
    fernet = Fernet(os.environ["ENCRYPTION_KEY"])

    def get_installation_token(repo_owner: str, repo_name: str):
        # Get these from your GitHub App settings
        app_id = os.environ["APP_ID"]
        private_key = base64.b64decode(os.environ["GITHUB_APP_PRIVATE_KEY"]).decode()
        
        # Generate JWT
        now = int(time.time())
        payload = {
            "iat": now,
            "exp": now + 600,  # 10 minutes
            "iss": app_id
        }
        jwt_token = encode(payload, private_key, algorithm="RS256")

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        response = requests.get(
            "https://api.github.com/app/installations",
            headers=headers
        )  
        installations = response.json()

        # Find the installation for this repository
        for installation in installations:
            if installation["account"]["login"] == repo_owner:
                # Get repositories for this installation
                installation_id = installation["id"]

                token_response = requests.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers=headers
                )

                installation_token = token_response.json()["token"]
                
                # Use the correct endpoint for getting installation repositories
                repo_headers = {
                    "Authorization": f"Bearer {installation_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
                repo_response = requests.get(
                    f"https://api.github.com/installation/repositories",  # Changed this URL
                    headers=repo_headers
                )
                
                repos = repo_response.json().get("repositories", [])
                if not repos:
                    repos = repo_response.json()
                
                # Check if this repository is in the installation
                for repo in repos:
                    if repo["name"] == repo_name:
                        return installation_token
                
        raise Exception(f"No installation found for {repo_owner}/{repo_name}")
    

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

        print("=== WEBHOOK CALLED ===")
        print(f"Headers: {request.headers}")
        print(f"Time: {time.time()}")

        delivery_id = request.headers.get("X-GitHub-Delivery")
        if not delivery_id:
            return {"status": "ignored", "reason": "no delivery ID"}
        
        if delivery_id in processed_deliveries:
            print(f"Ignoring duplicate webhook delivery: {delivery_id}")
            return {"status": "ignored", "reason": "duplicate delivery"}
        
        processed_deliveries.add(delivery_id)
        
        if len(processed_deliveries) > 100:
            processed_deliveries.clear()
        

        payload = await request.json()
        event = request.headers.get("X-GitHub-Event")
        
        if event != "issue_comment" or payload.get("action") != "created":
            return {"status": "ignored", "reason": "not a new PR comment"}
            
        if "pull_request" not in payload.get("issue", {}):
            return {"status": "ignored", "reason": "not a PR comment"}
            
        if "@tech-lead-bot" not in payload.get("comment", {}).get("body", ""):
            return {"status": "ignored", "reason": "bot not mentioned"}

        context = WebhookContext(payload)
        installation_token = get_installation_token(context.repo_owner, context.repo_name)


        print(f"Processing webhook for {context.commenter} on PR #{context.pr_number}")

        is_authenticated, auth_result = await authenticate_user(context)
        
        print("is_authenticated: ", is_authenticated)
        print("auth_result: ", auth_result)

        if not is_authenticated:
            html_content = f"""
            <html>
                <head>
                    <title>GitHub Authorization</title>
                    <script>
                        window.open("{auth_result}", "_blank");
                    </script>
                </head>
                <body>
                    <p>Opening GitHub authorization page in a new tab...</p>
                    <p>If the page doesn't open automatically, <a href="{auth_result}" target="_blank">click here</a>.</p>
                </body>
            </html>
            """
            
            write_status_comment(context.repo_owner, context.repo_name, context.pr_number, f"@{context.commenter} Please authorize the bot by clicking this link: {auth_result}", installation_token)
                
            return HTMLResponse(content=html_content)
 
        # User is authenticated, proceed with bot functionality
        try:
            write_status_comment(context.repo_owner, context.repo_name, context.pr_number, "Thinking...", installation_token)

            count = scrape.remote(
                username=context.requested_user, 
                repo_owner=context.repo_owner, 
                repo_name=context.repo_name, 
                force_reload=context.force_reload, 
                pr_number=context.pr_number, 
                commenter=context.commenter, 
                token=installation_token)
            
            print(f"Scraped {count} comments for {context.requested_user}")

            if count == -1:
                write_status_comment(context.repo_owner, context.repo_name, context.pr_number, "User already exists. Skipping fine-tuning.", installation_token)
            elif count == 0:
                return {"status": "success", "samples": 0}

            finetune.remote(
                username=context.requested_user,
                repo_owner= context.repo_owner, 
                repo_name=context.repo_name, 
                force_reload=context.force_reload)
            
            print("Finished fine-tuning")

            webhook_functionality(
                repo_owner=context.repo_owner,
                repo_name=context.repo_name,
                pr_number=context.pr_number,
                commenter=context.commenter,
                requested_user=context.requested_user,
                token=installation_token
            )

            return {"status": "success", "samples": count}
        except Exception as e:
            write_status_comment(context.repo_owner, context.repo_name, context.pr_number, "Something went wrong. Please try again.", installation_token)
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
            return {"status": "success", "message": "User authenticated. You may now close this tab, and return to the PR."}

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
    token: str
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
        review_and_comment(requested_user, repo_owner, repo_name, pr_number, file_path, commenter, token)
    return {"status": "scheduled"}
    