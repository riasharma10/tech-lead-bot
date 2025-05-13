import requests
import json
import time
import os

from collections import defaultdict 
from github_actions import write_status_comment

from common import (
    SYSTEM_PROMPT,
    get_user_data_path,
    get_user_model_path,
    app,
    output_vol,
    VOL_MOUNT_PATH,
    base_image,
    HOURS,
)
import modal


class GitHubPRScraper:
    def __init__(self, token, owner, repo):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        self.base_url = "https://api.github.com"
        
    def get_all_prs(self, state="all", max_pages=None):
        """Get all PRs in the repository."""
        prs = []
        page = 1
        
        while True:
            url = f"{self.base_url}/repos/{self.owner}/{self.repo}/pulls"
            params = {"state": state, "page": page, "per_page": 100}
            
            response = requests.get(url, headers=self.headers, params=params)
            
            if response.status_code != 200:
                print(f"Error fetching PRs: {response.status_code}")
                print(response.json())
                break
                
            batch = response.json()
            if not batch:
                break
                
            prs.extend(batch)
            print(f"Fetched page {page}, got {len(batch)} PRs")
            
            page += 1
            if max_pages and page > max_pages:
                break
                
            # Respect GitHub's rate limits
            if 'X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) < 10:
                reset_time = int(response.headers['X-RateLimit-Reset'])
                sleep_time = reset_time - time.time() + 5
                if sleep_time > 0:
                    print(f"Rate limit approaching, sleeping for {sleep_time} seconds")
                    time.sleep(sleep_time)
        
        return prs
    
    def get_pr_review_comments(self, pr_number):
        """Get review comments for a specific PR."""
        comments = []
        page = 1
        
        while True:
            url = f"{self.base_url}/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments"
            params = {"page": page, "per_page": 100}
            
            response = requests.get(url, headers=self.headers, params=params)
            
            if response.status_code != 200:
                print(f"Error fetching PR review comments: {response.status_code}")
                print(response.json())
                break
                
            batch = response.json()
            if not batch:
                break
                
            comments.extend(batch)
            
            page += 1
            
            # Respect GitHub's rate limits
            if 'X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) < 10:
                reset_time = int(response.headers['X-RateLimit-Reset'])
                sleep_time = reset_time - time.time() + 5
                if sleep_time > 0:
                    print(f"Rate limit approaching, sleeping for {sleep_time} seconds")
                    time.sleep(sleep_time)
        
        return comments
    
    def get_pr_files(self, pr_number):
        """Get files changed in a specific PR."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/pulls/{pr_number}/files"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code != 200:
            print(f"Error fetching PR files: {response.status_code}")
            print(response.json())
            return []
            
        return response.json()
    
    def get_file_content(self, commit_sha, filename):
        """Get file content at a specific commit."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/contents/{filename}"
        params = {"ref": commit_sha}
        
        response = requests.get(url, headers=self.headers, params=params)
        
        if response.status_code != 200:
            print(f"Error fetching file content: {response.status_code}")
            print(response.json())
            return None
            
        content_data = response.json()
        if "content" in content_data:
            import base64
            return base64.b64decode(content_data["content"]).decode("utf-8")
        return None
    
    def get_code_context(self, pr_number, comment):
        """Get code context for a comment."""
        # Get the relevant file at the commit the comment was made on
        path = comment.get("path")
        commit_id = comment.get("commit_id")
        
        if not path or not commit_id:
            return None
        
        # Get the file content
        content = self.get_file_content(commit_id, path)
        if not content:
            return None
        
        # Extract the specific code section being commented on
        lines = content.split("\n")
        
        print(f"Comment: {comment}")
        # Get diff hunk to understand context
        diff_hunk = comment.get("diff_hunk", "")

        start_line = comment.get("start_line", comment.get("line", 0))

        end_line = comment.get("line", start_line)
        
        if start_line is None:
            start_line = end_line

        print(f"Comment on {path} at lines {start_line}-{end_line}")

        # Try to get a reasonable context (few lines before and after)
        context_start = max(0, start_line - 5)
        context_end = min(len(lines), end_line + 5)
        
        # Extract the code context
        code_context = "\n".join(lines[context_start:context_end])
        
        return {
            "file": path,
            "commit": commit_id,
            "start_line": start_line,
            "end_line": end_line,
            "code": code_context,
            "diff_hunk": diff_hunk,
        }
    
    def create_prompt_response_pairs(self, prs=None, max_prs=None):
        """Create prompt/response pairs from PRs and comments."""
        if prs is None:
            prs = self.get_all_prs(max_pages=max_prs)
        elif max_prs:
            prs = prs[:max_prs]
            
        prompt_response_pairs = []
        user_pairs = defaultdict(list)
        
        for pr_index, pr in enumerate(prs):
            pr_number = pr["number"]
            pr_title = pr["title"]
            pr_user = pr["user"]["login"]
            
            print(f"Processing PR #{pr_number}: {pr_title} by {pr_user} ({pr_index+1}/{len(prs)})")
            
            # Get all review comments for this PR
            comments = self.get_pr_review_comments(pr_number)
            
            if not comments:
                continue
                
            # Process each comment
            for comment in comments:
                comment_user = comment["user"]["login"]
                comment_body = comment["body"]
                
                # Skip empty comments
                if not comment_body.strip():
                    continue
                    
                # Get code context for this comment
                code_context = self.get_code_context(pr_number, comment)
                
                if not code_context:
                    continue
                
                # Create prompt (code context) and response (comment)
                prompt = f"File: {code_context['file']}\nCode:\n{code_context['code']}"
                response = comment_body
                
                pair = {
                    "user": comment_user,
                    "pr_number": pr_number,
                    "pr_title": pr_title,
                    "file": code_context['file'],
                    "commit": code_context['commit'],
                    "prompt": prompt,
                    "response": response,
                    "metadata": {
                        "comment_id": comment["id"],
                        "comment_url": comment["html_url"],
                        "pr_url": pr["html_url"],
                        "created_at": comment["created_at"],
                    }
                }
                
                prompt_response_pairs.append(pair)
                user_pairs[comment_user].append(pair)
        
        return prompt_response_pairs, user_pairs
    
    def save_prompt_response_pairs(self, output_dir="output"):
        """Save prompt/response pairs to files."""
        os.makedirs(output_dir, exist_ok=True)
        
        # Get all pairs
        pairs, user_pairs = self.create_prompt_response_pairs()
        
        # Save all pairs to a single file
        with open(f"{output_dir}/all_pairs.json", "w") as f:
            json.dump(pairs, f, indent=2)
            
        # Save pairs by user
        for user, user_data in user_pairs.items():
            user_dir = os.path.join(output_dir, "by_user")
            os.makedirs(user_dir, exist_ok=True)
            
            with open(f"{user_dir}/{user}.json", "w") as f:
                json.dump(user_data, f, indent=2)
                
        print(f"Saved {len(pairs)} prompt/response pairs from {len(user_pairs)} users to {output_dir}")
        return pairs, user_pairs



# Scraping Module
@app.function(
    image=base_image,
    volumes={VOL_MOUNT_PATH: output_vol},
    timeout=2 * HOURS,
)
def scrape(username: str, repo_owner: str, repo_name: str, force_reload: bool, pr_number: int, commenter: str, token: str) -> int:
    """Scrape GitHub PR comments for a user.
    
    Args:
        username: GitHub username to scrape comments for
        repo_owner: Owner of the repository
        repo_name: Name of the repository
        token: GitHub OAuth token for authentication
        
    Returns:
        Number of examples collected
    """
    import pandas as pd
    from tqdm import tqdm

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }


    output_dir = get_user_model_path(username, repo_name)
    if output_dir.exists() and (output_dir / "epoch_1").exists() and not force_reload:
        print(f"Data already exists for {username}/{repo_name}")
        return -1
    
    # Fetch PRs
    print(f"Fetching PRs for {repo_owner}/{repo_name}")
    prs = []
    page = 1
    num_processed_prs = 0


    scraping_message = f"We are scraping the PRs for {username} now..."
    write_status_comment(repo_owner, repo_name, pr_number, scraping_message, token)
    
    while True:
        response = requests.get(
            f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls",
            headers=headers,
            params={"state": "all", "page": page, "per_page": 100}
        )
        
        if response.status_code != 200:
            print(f"Error fetching PRs: {response.status_code}")
            break
            
        batch = response.json()
        if not batch:
            break
            
        if len(batch) == 0:
            break
        
        prs.extend(batch)
        print(f"Fetched page {page}, got {len(batch)} PRs")
        
        page += 1
        if page > 30:  # Limit to first 3000 PRs (30 pages of 100)
            break
    
    examples = []
    print(f"Processing {len(prs)} PRs")
    
    # Process each PR
    for pr in tqdm(prs):
        pr_number_iteration = pr["number"]
        
        # Get PR review comments
        review_comments = []
        page = 1
        
        while True:
            response = requests.get(
                f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number_iteration}/comments",
                headers=headers,
                params={"page": page, "per_page": 100}
            )
            
            if response.status_code != 200:
                print(f"Error fetching PR comments: {response.status_code}")
                break
                
            batch = response.json()
            if not batch:
                break
                
            review_comments.extend(batch)
            page += 1
        
        # Filter comments by username
        for comment in review_comments:
            print(f"Processing comment {comment}")
            if comment["user"]["login"] == username:
                print("found one match")
                num_processed_prs += 1
                # Get file content and context
                path = comment.get("path")
                commit_id = comment.get("commit_id")
                
                if not path or not commit_id:
                    print("no path or commit id")
                    continue
                
                # Get file content at commit
                file_response = requests.get(
                    f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents/{path}",
                    headers=headers,
                    params={"ref": commit_id}
                )
                
                if file_response.status_code != 200:
                    print(f"no file response. file response: {file_response}, status code: {file_response.status_code}")
                    continue
                    
                content_data = file_response.json()
                if "content" in content_data:
                    print("has content")
                    import base64
                    file_content = base64.b64decode(content_data["content"]).decode("utf-8")
                    
                    # Parse line numbers from diff hunk
                    diff_hunk = comment.get("diff_hunk", "")
                    start_line = comment.get("start_line", comment.get("original_line", None))
                    end_line = comment.get("line", start_line)
                    
                    # If we can't get line numbers directly, parse from diff hunk
                    if diff_hunk and (start_line is None or end_line is None):
                        import re
                        match = re.search(r"@@ -\d+,\d+ \+(\d+),\d+ @@", diff_hunk)
                        if match:
                            start_line = int(match.group(1))
                            # Count lines in diff_hunk to estimate end_line
                            end_line = start_line + len(diff_hunk.split("\n")) - 2  # -2 for header and slack
                    
                    # Set sensible defaults if still missing
                    start_line = start_line or end_line
                    end_line = end_line or start_line
                    
                    # Extract context (expand context to include more lines)
                    lines = file_content.split("\n")
                    context_start = max(0, int(start_line) - 10)
                    context_end = min(len(lines), int(end_line) + 10)
                    code_context = "\n".join(lines[context_start:context_end])
                    
                    # Create training example
                    example = {
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT.replace("{USERNAME}", username)},
                            {"role": "user", "content": f"File: {path}\n\nCode:\n```\n{code_context}\n```"},
                            {"role": "assistant", "content": comment["body"]}
                        ]
                    }
                    
                    examples.append(example)

    if len(examples) == 0:
        no_examples_message = f"No PR comments found for {username}. Please use the bot with users that have more PRs."
        write_status_comment(repo_owner, repo_name, pr_number, no_examples_message, token)
        return 0

    # Save data
    data_path = get_user_data_path(username, repo_name)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(data_path, "w") as f:
        json.dump(examples, f, indent=2)
    
    output_vol.commit()
    
    print(f"Collected {len(examples)} examples for {username}")
    return len(examples)