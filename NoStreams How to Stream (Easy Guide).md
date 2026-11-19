# NoStreams: How to Stream (Easy Guide)

Going live takes three things: a free account on the NoStreams site, a one-time install of Python and ffmpeg, and one command. After setup, every stream is: start the script, paste your key, name your stream, go.

## 1. One-time setup (about 10 minutes)

Do this once per PC (Windows).

1. **Install Python 3.8 or newer** from [python.org/downloads](https://www.python.org/downloads/). On the first installer screen, tick **Add python.exe to PATH**.
2. **Install ffmpeg.** Open Command Prompt (press the Windows key, type `cmd`, Enter) and run: `winget install ffmpeg`
3. **Install the audio helpers** (this lets the stream carry your PC's sound): `pip install pyaudiowpatch numpy`
4. **Close and reopen Command Prompt**, then check everything worked: `python --version` and `ffmpeg -version`. Both should print a version number.

## 2. Get your stream key

1. Open <https://nostreams.tail1be5a7.ts.net> in your browser.
2. Sign up for an account.
3. Copy your **stream key**. You can sign in later to see it again or generate a new one.

Treat the key like a password. Anyone who has it can stream as you.

## 3. Start streaming

1. Put `NoStream-Client.py` in its own folder, for example `C:\NoStreams`.
2. Open Command Prompt **in that folder**: open the folder in File Explorer, click the address bar, type `cmd` and press Enter.
3. Run: `python NoStream-Client.py`
4. Answer the three questions:
   - **Stream key:** paste it and press Enter. Nothing shows as you type; that is on purpose, so it never appears on your stream.
   - **Stream name:** type a title, or press Enter to skip.
   - **Viewer delay:** press Enter for 0 (as live as possible), or type a number of seconds up to 300.
5. Wait for this line: `[ok] ffmpeg is producing video segments - the stream is live.`
6. Open the address it prints to watch. Your own page is `https://nostreams.tail1be5a7.ts.net/<your username>/`.

**To stop:** press **Ctrl+C** once and wait for `Stream ended`. It finishes uploading and the server saves a recording. Closing the window instead can lose the end of your stream.

## 4. Make it one double-click (optional)

Open Notepad, paste the three lines below, and save the file as `Start NoStreams.bat` in the same folder as the script (set "Save as type" to **All files**).

```
@echo off
cd /d "%~dp0"
python NoStream-Client.py
pause
```

Double-click it to start. The `pause` line keeps the window open if something goes wrong, so you can read the message.

To skip pasting the key every time, run `setx PCSTREAM_KEY your-key-here` once in Command Prompt, then reopen it. The key is then stored in plain text on your Windows account, so only do this on a PC you trust.

## 5. Common options

Add these after the command, for example `python NoStream-Client.py --audio both --height 1080`.

| Option | What it does |
| --- | --- |
| `--audio pc` | PC sound only (default) |
| `--audio both` | PC sound plus your microphone |
| `--audio mic` | Microphone only |
| `--audio none` | Video only |
| `--source webcam --video-dev "Integrated Camera"` | Stream your webcam instead of the screen |
| `--height 1080` | Higher picture quality (default 720; needs more upload speed) |
| `--bitrate 4000k` | More detail per second (default 2500k) |
| `--title "Speedrun night"` | Set the name without being asked |
| `--no-record` | Live only, no saved recording |
| `--list-devices` | Show your cameras and microphones, then exit |
| `--debug` | Show ffmpeg's full output when troubleshooting |

## 6. If something goes wrong

| You see | What it means and what to do |
| --- | --- |
| `ffmpeg not found on PATH` | ffmpeg is not installed, or Command Prompt was opened before installing. Run `winget install ffmpeg`, then close and reopen Command Prompt. |
| `Windows audio needs: pip install pyaudiowpatch numpy` | Run that command, or start with `--audio none` to stream without sound. |
| `That stream key wasn't accepted` | The key was mistyped or regenerated. Sign in on the website, copy the current key, and try again. |
| `Can't reach ...` | The server is off or unreachable. Check your internet, then ask the server owner to confirm it is running. |
| `20s and ffmpeg still hasn't produced any segments` | Run again with `--debug` and read the last few lines. Often another program has the camera or screen locked. |
| `python` is not recognized | Python was installed without "Add to PATH". Rerun the installer, choose Modify, and tick the PATH option. |
| Viewers hear no sound | Check the PC is actually playing audio, or try `--audio both` and speak to test the mic. |

## 7. Before you go live

- Your stream is public to anyone who has the link, so close private windows, notifications and password managers first.
- Use `--region 1280x720+0+0` to share only part of one monitor.
- Recordings stay on the server under your username, so ask the server owner if you want one removed.
