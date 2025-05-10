# 🧠 tech-lead-bot

**`tech-lead-bot`** is your automated code review assistant — trained to review pull requests just like your tech lead would. It observes your past code reviews, learns your tone and style, and generates new review comments tailored to your coding habits.

It is designed to help teams scale high-quality feedback, save engineers' time, and ensure consistency in code quality.

---

## 📦 What It Does

When you comment on a pull request with:

```
@tech-lead-bot {github-username}
```

The bot springs into action:

1. **GitHub Authentication**  
   The requesting user is prompted to authenticate using GitHub OAuth, ensuring access to necessary repo data.

2. **Data Collection: Personalized Training Set**  
   We scrape the specified `{github-username}`’s PR history in the target repository. This includes:
   - Review comments
   - Diff hunks and file context
   - Metadata like line numbers and commit hashes

3. **Model Fine-Tuning**  
   We fine-tune a LoRA adapter on top of Meta’s LLaMA 3 8B model using the collected examples. The adapter is stored per user and repository, allowing reuse on future PRs.

4. **Automated Code Review**  
   Each diff hunk in the active PR is passed to the model. It generates a one-line review comment in the target user’s style, with a snarky tone, and posts it inline.

5. **GitHub Integration**  
   The bot uses GitHub’s REST API to post review comments at the correct positions in the PR diff.

---

## ⚙️ Setup: How to Use

To enable the bot on your GitHub repository:

1. **Add a Webhook**
   - Go to **Settings → Webhooks → Add webhook**
   - **Payload URL**:  
     `https://riassharma10--github-codereview-bot-api-dev.modal.run/webhook`
   - **Content type**:  
     `application/json`
   - **Events** (select these):
     - `Discussion comments`
     - `Issue comments`
     - `Pull request review comments`

2. **Trigger the Bot in a Comment**

In any PR, add a comment:
```
@tech-lead-bot {username}
```

You can force the bot to re-scrape data and re-train the model by adding the `--force-reload` flag:
```
@tech-lead-bot {username} --force-reload
```

---

## 🧠 Model + Inference

- **Model**: Meta LLaMA 3 8B Instruct
- **Fine-tuning**: LoRA adapters using TorchTune
- **Serving**: vLLM for low-latency inference
- **GPU**: H100 for training, L40S for inference (on Modal)
- **Prompting Style**: OpenAI-compatible chat format with a custom snarky `system` prompt

Each user has a unique adapter trained on their past PR comments. If a model already exists for a user and repository, it will be reused unless `--force-reload` is specified.

---

## 💾 Caching and Data Handling

- **Caching**  
  All scraped PR data and trained adapters are cached in a mounted Modal volume. This avoids redundant scraping and retraining for frequent users.

- **Force Reload**  
  Use the `--force-reload` flag to override the cache. This is useful if a user has left many new PR comments since their last review.

- **Security & Privacy**  
  - OAuth tokens are encrypted using a Fernet key stored as a Modal secret.
  - GitHub data is cached **only for the session** and is **not stored persistently**.
  - No code or comments are retained beyond the bot's operation lifecycle.

---

## 🛠️ Tech Stack

- **Infrastructure**: [Modal](https://modal.com)
- **LLM**: [Meta LLaMA 3 8B](https://ai.meta.com/llama/)
- **Training**: [TorchTune](https://github.com/pytorch/torchtune)
- **Serving**: [vLLM](https://github.com/vllm-project/vllm)
- **Authentication**: GitHub OAuth
- **Diff Parsing**: Unified diff patch parsing + line mapping

---

## 📫 Contact

For support or questions, contact `@riassharma10`.

Happy reviewing!
