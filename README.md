# Cliphound

**Long streams in, Shorts out.** Paste a link to a Twitch, YouTube or Kick stream. Cliphound finds the best moments, cuts them into vertical Shorts with big word-by-word captions, and can post them straight to your YouTube channel.

It's free, open source, and runs on your own computer. No account, no subscription, no limit on how many Shorts you make.

- [What it does](#what-it-does)
- [Set it up (about 10 minutes)](#set-it-up-about-10-minutes)
- [Make your first Shorts](#make-your-first-shorts)
- [Optional: let AI pick the moments](#optional-let-ai-pick-the-moments)
- [Optional: post to YouTube](#optional-post-to-youtube)
- [Problems and fixes](#problems-and-fixes)
- [Clipping other people's streams](#clipping-other-peoples-streams)

## What it does

- **Fast on long streams.** A finished YouTube stream is read from YouTube's own transcript, so even a 9 hour stream is scanned in seconds. Twitch, Kick and live streams are scanned from their audio.
- **Finds the good parts.** It looks for where the streamer gets loud, talks fast, repeats themselves ("no no no", "go go go") and says things like "no way" or "let's go".
- **AI picking (optional).** Connect ChatGPT or an OpenAI key and a model reads what was said, picks the strongest moments, trims them and writes the titles.
- **Ready-to-post Shorts.** 1080x1920 vertical video, captions that highlight each word, the streamer's name on screen, and balanced volume.
- **Edit before you post.** Change any title or description, remove the Shorts you don't like, then press Post.
- **One exact moment.** Add a time to the link (like `?t=1h2m3s`) and you get a Short of just that moment.

## Set it up (about 10 minutes)

You need two free tools: **uv** (it runs Cliphound and installs everything it needs, including Python) and **ffmpeg** (it cuts the video). You only do this once.

### On a Mac

1. Open **Terminal**. Press `Cmd + Space`, type `Terminal`, press Enter.
2. Install **uv**. Copy this line, paste it into Terminal and press Enter:
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. Install **ffmpeg**. If you don't have Homebrew yet, install it first from [brew.sh](https://brew.sh) (it's one line, the page shows you). Then run:
   ```sh
   brew install ffmpeg
   ```
4. **Close Terminal and open it again**, so it picks up what you just installed.

### On Windows

1. Open **PowerShell**. Press the Windows key, type `PowerShell`, press Enter.
2. Install **uv**:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. Install **ffmpeg**:
   ```powershell
   winget install --id Gyan.FFmpeg -e
   ```
4. **Close PowerShell and open it again.**

### On Linux (Ubuntu, Debian and similar)

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install ffmpeg
```

Then close the terminal and open a new one.

### Get Cliphound

**Easiest:** on this page, click the green **Code** button, then **Download ZIP**. Unzip it somewhere you'll find it again, like your Documents folder. The folder will be called `cliphound-main`.

**If you use git:**
```sh
git clone https://github.com/Ash20pk/cliphound
```

### Start it

- **Mac or Linux:** in Terminal, go into the folder and run the start script:
  ```sh
  cd ~/Documents/cliphound-main
  sh run.sh
  ```
  Use `cd cliphound` instead if you used git, and change the path if you put the folder somewhere else. Tip: type `cd ` with a space, then drag the folder into the Terminal window and press Enter.
- **Windows:** open the Cliphound folder and double-click **run.bat**.

The first start takes a few minutes while it installs everything. When you see `Application startup complete`, open **http://127.0.0.1:8765** in your browser.

Keep that window open while you use Cliphound. To stop it, press `Ctrl + C` or close the window. Next time, run the start script again and it's up in seconds.

## Make your first Shorts

1. Copy the link to a stream or video: a YouTube stream, a Twitch VOD (`twitch.tv/videos/...`) or a Kick video.
2. Paste it into the big box at the top. A preview card shows the stream so you know it's the right one.
3. Pick how many Shorts you want and how long each should be.
4. Press the orange button (it shows the number, like **Make 5 Shorts**).

Your Shorts appear under **Your Shorts** as they're made. The strip above them shows the whole stream, with orange marks where each Short came from. Each Short has:

- **Download** to save the video file.
- **Edit** to change its title and description.
- **Post** to put it on your YouTube channel (once you've connected YouTube, see below).
- **Remove** if you don't want it.

The very first job also downloads the speech-to-text model (about 1.6 GB), so it takes a little longer.

## Optional: let AI pick the moments

Without AI, Cliphound picks moments by how lively the stream is. With AI, a model reads what was actually said, picks the moments that make good Shorts and writes the titles. You can use either:

- **Your ChatGPT plan.** Open **Settings > AI picking** and click **Sign in with ChatGPT**. Needs ChatGPT Plus or Pro.
- **An OpenAI API key.** Create one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys), then paste it into **Settings > AI picking**. You pay OpenAI for what you use.

Also fill in **What your channel is about** in the same section. The AI uses it to decide what fits your channel.

## Optional: post to YouTube

To post, Cliphound needs permission from Google. Google makes every app do this through a free "Cloud project" that you own. It's a one-time setup of about 10 minutes, and none of it costs money.

### 1. Create the project

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and sign in with the Google account that owns your YouTube channel.
2. At the top, click the project picker (next to the Google Cloud logo), then **New project**. Name it `Cliphound` and click **Create**. Make sure the new project is selected at the top.

### 2. Turn on the YouTube API

1. Open the menu (top left) and go to **APIs & Services > Library**.
2. Search for **YouTube Data API v3**, click it, and click **Enable**.

### 3. Set up the sign-in screen

1. In the menu, go to **APIs & Services > OAuth consent screen** (it may be called **Google Auth Platform**). Click **Get started**.
2. **App name:** `Cliphound`. **User support email:** your email. Click **Next**.
3. **Audience:** choose **External**. Click **Next**.
4. **Contact information:** your email. Click **Next**, tick the agreement, click **Create**.
5. Go to **Audience**, find **Test users**, click **Add users**, add your own Gmail address and save. If your channel is a brand account, add the Google account you use to manage it.

### 4. Create the key file

1. Go to **Clients** (or **APIs & Services > Credentials**) and click **Create client** (or **Create credentials > OAuth client ID**).
2. **Application type:** **Desktop app**. Name it `Cliphound`. Click **Create**.
3. Click **Download JSON**. Keep this file private, it's your app's key.

### 5. Connect Cliphound

1. In Cliphound, open **Settings > YouTube** and click **Add OAuth client file**. Pick the file you just downloaded.
2. Click **Sign in with Google** and choose the channel the Shorts should go to.
3. Google shows **"Google hasn't verified this app"**. That's expected, because it's your own app. Click **Continue**.
4. Tick every permission box and click **Continue**.

Settings now shows your channel. Press **Post** on any Short to publish it. To post automatically as soon as Shorts are made, turn on **Post automatically when a link finishes** in **Settings > New links**. That only happens for streamers you've marked as giving permission.

### Two things Google does that you should know

- **Your first uploads will be private.** YouTube keeps videos from new, unreviewed apps private until Google audits the app. Cliphound asks for public, and each Short shows what YouTube actually did. Until then, click **Open in Studio** on the Short and switch it to Public yourself. To lift this for good, apply for the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form) for your Cloud project.
- **Sign-in lasts 7 days while the app is in "Testing".** When it expires, click **Sign in with Google** again. Publishing the app on the consent screen's **Audience** page removes the limit, but Google asks for a home page and a privacy policy first.

YouTube also has a daily upload limit per project. If you hit it, Cliphound tells you and the rest keep their Post button for tomorrow.

## Problems and fixes

| What you see | What to do |
|---|---|
| `command not found: uv` | Close the terminal and open a new one. If it still happens, run the uv install line again. |
| An error mentioning `ffmpeg` | ffmpeg isn't installed, or the terminal hasn't picked it up yet. Install it (see setup), then open a new terminal. |
| The page won't open | Check the Cliphound window is still running and says `Application startup complete`. Use `http://127.0.0.1:8765`. |
| `address already in use` | Cliphound is already running in another window. Use that one, or close it first. |
| "That link didn't open" | The video may be private, members-only or age-restricted. Try another link. |
| "Live streams can only be clipped from YouTube" | For Twitch, wait until the stream ends and paste the VOD link. |
| Slow on Windows or Linux | Speech-to-text runs on the processor there, which is slower than on an Apple Silicon Mac. It still works. |
| "That Google account has no YouTube channel" | Sign in again and pick the channel (not just the account) when Google asks. |
| Google says "Access blocked" | Add your email under **Test users** (step 3.5 above). |
| A Short says "Private on YouTube" | Expected until your Cloud project passes Google's audit. Publish it from YouTube Studio. |

## Clipping other people's streams

Only post clips you have the right to post. Many streamers are happy to be clipped, and some pay clippers per view. Ask first, or join their clipping programme.

Cliphound adds each streamer to **Settings > Streamers** the first time you paste their link, with permission off. Automatic posting only happens once you turn their permission on, and posting by hand asks you to confirm. Every Short shows the streamer's name, and the default description credits and links to their channel.

## Where your stuff is kept

Everything stays in the `data` folder inside Cliphound: your settings, your sign-ins (only your user account can read them), downloaded audio (deleted after 7 days) and your Shorts. Cliphound only talks to the site you're clipping from, to YouTube when you post, and to OpenAI if you turn on AI picking. It only accepts requests from the browser on your own computer.

## For developers

```sh
uv sync
uv run pytest
```

The app is a small FastAPI server (`cliphound/app.py`) with a single-page UI (`cliphound/static/index.html`). Moments are found in `cliphound/analyze.py`, videos are fetched in `cliphound/download.py` and `cliphound/live.py`, and Shorts are rendered with ffmpeg in `cliphound/render.py`.

## License

MIT
