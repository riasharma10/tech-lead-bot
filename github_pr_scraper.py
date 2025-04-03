import requests
import json
import time
import os
from env_vars import GITHUB_TOKEN

from collections import defaultdict

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


# Example usage
if __name__ == "__main__":
    # Replace with your GitHub token and target repository details
    token = GITHUB_TOKEN
    owner = "riasharma10"
    repo = "cis550-fitcheck"
    
    scraper = GitHubPRScraper(token, owner, repo)
    
    # Option 1: Get all PRs and create pairs
    pairs, user_pairs = scraper.save_prompt_response_pairs(output_dir="github_pr_data")
    
    # Option 2: Get specific PRs and create pairs
    # specific_prs = scraper.get_all_prs(state="closed", max_pages=1)  # Get just the first page of closed PRs
    # pairs, user_pairs = scraper.create_prompt_response_pairs(prs=specific_prs)
    
    # Print some stats
    print(f"Total pairs: {len(pairs)}")
    print(f"Users with comments: {len(user_pairs)}")
    for user, user_data in user_pairs.items():
        print(f"  {user}: {len(user_data)} comments")