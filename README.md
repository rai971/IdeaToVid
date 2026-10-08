# The Finance Brief Studio

A local-first editorial workbench for collecting U.S. finance and economy headlines, drafting short reel scripts, and recording a human approval decision. It deliberately does not publish anything to Facebook.

## Run locally

Requires Python 3. The reel renderer uses `imageio-ffmpeg`; install it with `python3 -m pip install -r requirements.txt`.

```sh
python3 app.py
```

Open <http://127.0.0.1:8787>. The SQLite database is created as `finance_brief.db` in this folder.

## Daily flow

1. Select **Find today's stories** to check the Federal Reserve, Bureau of Labor Statistics, and SEC feeds, alongside several U.S.-focused Google News searches and Google Trends U.S. search-interest signals.
2. Open the publisher's original report and check the latest context. Google Trends is a directional search-interest signal, not a representative poll. Feed headlines and summaries are leads, not independently fact-checked stories. The dashboard shows which feeds responded and which failed.
3. Create a draft and edit every claim, number, date, and caption against the source.
4. Generate narration, use AI or topic rules to find Pixabay visuals, then select up to five clips. Choose **Plan visuals for narration** to assign each spoken beat to a clip and preview its proposed timeline and source segment. Render the reel preview and check the visual fit, names, numbers, and caption sync.
5. Approve or reject the finished reel. Approval is recorded locally. Downloaded Pixabay source clips are deleted after a successful render; the final reel and source-credit/storyboard details remain. Rejected ideas can be permanently deleted from the Rejected view.

The app supports Gemini and OpenAI for script drafting, Pixabay search terms, and clip-to-narration storyboards. Configure the provider, model names, and credentials from **Settings** in the app; credentials are saved locally to `.env` and are never sent back to the browser. The default is `AI_PROVIDER=auto`, which prefers Gemini and then OpenAI. The default Gemini model is `gemini-3.1-flash-lite`; Google lists a free tier for selected Gemini models, subject to current quotas. Free-tier content may be used by Google to improve its products, so requests contain only public story information. Choose **Built-in fallback only** to use template scripts and topic matching without an LLM. OpenAI API charges are separate from a ChatGPT plan. The storyboard model sees narration text and Pixabay tags/search terms, not video frames, so inspect the rendered reel for fit. Create a Gemini key in [Google AI Studio](https://aistudio.google.com/app/apikey). Local Kokoro narration is installed for this Mac and needs no service key. The Kokoro runtime and model are stored under `work/`; generation runs locally using a consistent American English voice. Set `KOKORO_VOICE` in `.env` to change the preset. `VOICE_PROVIDER=kokoro` is the default; set it to `elevenlabs` to use configured ElevenLabs credentials instead. Add `PIXABAY_API_KEY` only if you want stock-footage search; the original graphic background works without it. Restart the app after changing `.env`. Get an ElevenLabs API key from [ElevenLabs API settings](https://elevenlabs.io/app/settings/api-keys), choose a voice you have rights to use and copy its voice ID, and get an optional Pixabay API key from [Pixabay API docs](https://pixabay.com/api/docs/). Keys stay on this computer and are never sent to the browser. Never commit `.env` or paste keys into chat.

## Current scope

- Implemented: dated idea archive and filtering, rejected-idea deletion, dedicated reel-production queue, source feed retrieval, manual story entry, editable scripts/captions, configurable Gemini/OpenAI drafting, visual keyword selection and narration-to-clip storyboard, local Kokoro narration with optional ElevenLabs override, optional Pixabay portrait-clip search, selection of up to five clips per reel, original graphic background, FFmpeg 9:16 video rendering with timed clip segments and burned-in captions, preview, and approval gating. The final MP4 is stored in `assets/`; downloaded source clips are removed after rendering.
- Not implemented yet: automatic daily scheduling or Facebook connection and publishing. Approval requires a rendered preview, but does not publish the reel.
- A disclaimer is not a substitute for accurate reporting. Review sources and rights for each item. Automated checks cannot guarantee Facebook policy compliance.

## Before enabling external services

- ElevenLabs' free plan is for non-commercial use and requires attribution; for commercial use, generate under a plan with commercial rights. Confirm the current terms before publishing. https://help.elevenlabs.io/hc/en-us/articles/13313564601361-Can-I-publish-the-content-I-generate-on-the-platform
- Kokoro model weights are published under Apache-2.0. Review the model and voice terms when distributing generated content. https://huggingface.co/hexgrad/Kokoro-82M
- Pixabay's API and media use are subject to its current rate limits, caching requirements, and content license. The studio caches searches and keeps the creator and source link with each selected clip. Review rights for recognizable people, brands, and locations. https://pixabay.com/api/docs/ and https://pixabay.com/service/terms/
- For Meta, use the official Facebook Page Reels publishing flow and only the permissions that the Page and app are eligible for. Keep the approval step as a server-side requirement before a publish request can be made.
- Protect this local dashboard if you expose it beyond `127.0.0.1`; it has no login or multi-user protection.
