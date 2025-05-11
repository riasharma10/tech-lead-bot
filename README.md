# 🧠 tech-lead-code-reviewer

### 🏢 For Engineers:
Are you a software engineer who dreads code reviews from *that* tech lead — the one who leaves a million nits on every PR?  
What if you could anticipate their feedback before they even open your diff?

Introducing **`tech-lead-code-reviewer`** — an automated AI assistant that mimics your tech lead’s style and tone, helping you:

✅ Avoid looking clueless in reviews  
✅ Get feedback in seconds, not hours  
✅ Preempt nitpicks before they’re even written  

### 🏢 For Teams and Enterprises:

Your senior engineers shouldn't waste cycles on AI-generated code or junior PRs. Now they don’t have to.

✅ Reduce time-to-merge by an entire review cycle  
✅ Ship to production faster  
✅ Free up engineers to focus on what matters — like meetings 😉

---

## 💡 What It Does

**`tech-lead-code-reviewer`** is trained on your tech lead’s actual GitHub review comments and code context. It learns their style and provides personalized reviews for every diff hunk in your PR.

It’s built to scale high-quality code reviews while saving your team hours of back-and-forth.

---

## 📦 How to Use

Once your PR is ready, comment:

```
@tech-lead-bot {github-username}
```

And watch the bot go to work:

1. **Data Collection**  
   We scrape `{github-username}`’s review history in the current repository. Specifically:
   - All review comments (`pulls/:pr/comments`) left by the user across up to 3,000 PRs
   - For each comment: the exact file path, commit SHA, and diff hunk context
   - We fetch the file contents at the specific commit and extract the surrounding ~20 lines of code around each comment

2. **Model Fine-Tuning**  
   Using the extracted data, we construct prompt/response pairs:
   - Prompt: code context + file metadata
   - Response: actual comment the user wrote  
   These are used to fine-tune a LoRA adapter on Meta’s LLaMA 3 8B. Each adapter is cached per (user, repo) and reused automatically. We never store your source code beyond the session.

3. **Review Generation**  
   When invoked, we parse the PR's diff, split it into hunks, and apply your fine-tuned model to each. The model responds with a one-liner review styled like your tech lead’s past comments.

4. **GitHub Integration**  
   Comments are posted directly via GitHub’s REST API to the corresponding file and line in the PR. This happens automatically within seconds of the bot being called.

Want a fresh retrain? Add `--force-reload` to your comment:
```
@tech-lead-bot {github-username} --force-reload
```
This will be useful if there have been lots of new comments since calling the bot last. 
---

## ⚙️ Setup

### 1. **Install the GitHub App**
- Visit: [`https://github.com/apps/tech-lead-code-reviewer`](https://github.com/apps/tech-lead-code-reviewer)
- Click **Install**, select the repositories you want to enable
- Done ✅

### 2. **Trigger the Bot**

Comment on a PR:
```
@tech-lead-bot {github-username}
```

Optional: force retraining
```
@tech-lead-bot {github-username} --force-reload
```

---

## 🧠 Model & Inference

| Component         | Tech                                                                 |
|------------------|----------------------------------------------------------------------|
| Base Model       | Meta LLaMA 3 8B Instruct                                              |
| Tuning           | LoRA adapters with TorchTune                                          |
| Serving          | High-throughput inference via vLLM                                   |
| Hardware         | H100 (training), L40S (inference) on Modal infrastructure             |
| Prompt Format    | OpenAI-style chat               |

Each user gets their own adapter — reused automatically for subsequent reviews.

---

## 💾 Caching & Privacy

We take performance and security seriously. Here’s how we optimize for both:

- **Model Caching**
    - Each user’s LoRA adapter is cached by (GitHub username, repository) in a secure Modal volume. This allows the bot to reuse models across PRs without retraining — making subsequent reviews nearly instant.

- **Code Context Caching**
    - The scraped review data and file context used for fine-tuning is cached only within the session to accelerate training and prevent redundant GitHub API calls. This data is automatically discarded once the session ends unless --force-reload is used.

- **Force Reload**

    - Add --force-reload to a bot comment to:
    - Re-scrape the user’s latest PR history
    - Rebuild prompt-response pairs from scratch
    - Retrain the adapter, overwriting the cached model

- **Token Security**

    - GitHub OAuth tokens are encrypted using Fernet and stored as a Modal Secret.
    - Tokens are scoped to the authenticated user and used solely to fetch the required PR data.

- **Data Privacy**

    - We never persist source code, PR comments, or model outputs outside the session.
    - No user data is written to disk, exported, or shared across users or environments.
    - All processing happens in isolated Modal containers tied to your request.

---

## 🛠️ Tech Stack

- **Infra**: [Modal](https://modal.com)
- **LLM**: [LLaMA 3 8B](https://ai.meta.com/llama/)
- **Training**: [TorchTune](https://github.com/pytorch/torchtune)
- **Serving**: [vLLM](https://github.com/vllm-project/vllm)
- **Auth**: GitHub OAuth
- **Diff Parsing**: Custom unified diff + line mapper

---

## 📫 Contact

Questions? Feedback? Bugs?  
Reach out to [`@riassharma10`](https://github.com/riassharma10)

