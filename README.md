# Cliphound

Long streams in, Shorts out. Paste a Twitch, YouTube or Kick link and Cliphound finds the best moments, cuts them to vertical 9:16 with word-by-word captions, and posts them to your YouTube channel. It runs on your own computer, so there's no subscription and no upload limit on our side.

## What it does

- Scans the whole stream cheaply: it downloads the audio first, then fetches video only for the moments it picks.
- Finds moments by loudness against the stream's own baseline, chat activity when there's a replay, and hype words in the transcript.
- Optional AI picking: sign in with ChatGPT or paste an OpenAI API key, and a model reads the transcripts, picks the strongest moments, trims them and writes titles.
- Live YouTube streams work too, including ones longer than 12 hours.
- Add `?t=1h2m3s` to a link to clip one moment.
- Edit the title and description of any Short before you post it.

## Install

You need [uv](https://docs.astral.sh/uv/) and [ffmpeg](https://ffmpeg.org/).

```sh
git clone https://github.com/Ash20pk/cliphound
cd cliphound
./run.sh
```

Then open http://127.0.0.1:8765.

On Apple Silicon Macs transcription uses mlx-whisper and video uses the hardware encoder. On Linux, Windows and Intel Macs it uses faster-whisper and x264, which is slower but works the same. The first run downloads the Whisper model (about 1.5 GB).

## Posting to YouTube

Posting needs a Google OAuth client of your own. It's a one-time setup and free:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **YouTube Data API v3**.
2. Set up the OAuth consent screen (External is fine) and add yourself as a test user.
3. Create an OAuth client of type **Desktop app** and download its JSON file.
4. In Cliphound, open Settings > YouTube, add the file, then press **Sign in with Google** and pick your channel.

Two limits come from Google, not Cliphound:

- **Uploads stay private until Google audits your project.** YouTube locks every video uploaded through the API by an unaudited project to private. Cliphound asks for public and shows you what YouTube actually did. Until your project passes the [YouTube API Services audit](https://support.google.com/youtube/contact/yt_api_form), publish from YouTube Studio.
- **Test mode sign-ins last 7 days.** While your consent screen is in Testing, sign in again weekly, or publish the consent screen.

There's also a daily upload quota per Cloud project. When it runs out, Cliphound tells you and the remaining Shorts keep a Post button for the next day.

## Clipping other people's streams

Only post clips you have the right to post. Cliphound adds each streamer the first time you paste their link, with permission off. Automatic posting only happens for streamers whose permission you've turned on in Settings > Streamers, and posting by hand asks you to confirm. Every Short carries a credit line with the streamer's name, and the default description links to their channel.

## Where things live

Everything stays in the `data/` folder next to the app: settings, sign-ins (readable only by you), downloaded audio (cleared after 7 days) and your Shorts. Nothing is sent anywhere except the platform you're downloading from, YouTube when you post, and OpenAI if you turn on AI picking.

Cliphound only accepts requests from the browser on your own computer.

## Development

```sh
uv sync
uv run pytest
```

## License

MIT
