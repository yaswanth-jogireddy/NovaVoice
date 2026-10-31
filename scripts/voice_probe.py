"""Voice-mode probe: a synthetic caller speaks to a running server, exactly like the browser does.

    python scripts/voice_probe.py            (server running with MILESTONE>=2 on :8935)

Caller lines are synthesised with the project's own Sarvam TTS at 16 kHz, streamed in real time as
20 ms PCM frames between `caller_activity` / `caller_speech_end`, with silence frames while waiting
(a browser keeps sending mic audio). Agent audio is counted per reply and acknowledged with
`played`, as the browser does after playback. Reports what STT heard, what the agent said, the
audio bytes received and the browser-side delay "caller stops -> first agent audio".
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

FRAME = 640                    # 20 ms of 16 kHz PCM16
SILENCE = b"\x00" * FRAME
EXTRA = ""   # e.g. "&tier=standard&voice=el-us" (set from --tier / --voice)
LINES = ["What's my account balance?",
         "My customer ID is C 1 0 0 1. Date of birth 14 March 1992. Last four digits 7 3 0 6."]


async def synthesize(lines: list[str]) -> list[bytes]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from config import Settings
    from pipeline.tts import create_tts_processor
    settings = Settings(_env_file=ROOT / ".env").model_copy(update={"sarvam_tts_sample_rate": 16000})
    tts = create_tts_processor(settings, "sarvam")
    await tts.initialize()
    out = []
    try:
        for line in lines:
            tts.reset()
            out.append(b"".join([c async for c in tts.synthesize(line, language="en")]))
    finally:
        await tts.close()
    return out


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (as in Voice_Agent_temp tools/call_metrics.py)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(p / 100 * len(ordered) + 0.5) - 1))]


async def run_many(base: str, calls: int, out: Path | None) -> None:
    audio = await synthesize(LINES)
    print("caller audio:", [f"{len(a) / 32000:.1f}s" for a in audio])
    heard, server = [], []
    for n in range(calls):
        print(f"--- call {n + 1}/{calls}")
        h, s = await run(base, audio)
        heard += h
        server += s
    summary = {"calls": calls, "replies": len(heard),
               "caller_stop_to_first_audio_ms": {"p50": percentile(heard, 50), "p95": percentile(heard, 95),
                                                 "min": min(heard, default=None), "max": max(heard, default=None)},
               "server_reply_start_to_first_audio_ms": {"p50": percentile(server, 50), "p95": percentile(server, 95)},
               "samples_ms": heard}
    print(json.dumps(summary, indent=1))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=1), encoding="utf-8")


async def run(base: str, audio: list[bytes]) -> tuple[list[float], list[float]]:
    heard, server = [], []
    async with aiohttp.ClientSession() as http:
        await (await http.get(f"{base}/api/status")).read()
        url = f"{base.replace('http', 'ws')}/api/session?language=en&mode=voice&scenario=support{EXTRA}"
        async with http.ws_connect(url, origin=base, max_msg_size=0) as ws:
            replies: asyncio.Queue = asyncio.Queue()
            state = {"bytes": 0, "text": "", "first_audio_at": None}

            async def reader():
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        if state["first_audio_at"] is None:
                            state["first_audio_at"] = time.monotonic()
                        state["bytes"] += len(msg.data)
                        continue
                    data = json.loads(msg.data)
                    kind = data.get("type")
                    if kind == "transcript" and data.get("role") == "user":
                        print("   STT heard:", data["text"])
                    elif kind == "agent_text":
                        state["text"] = data["text"]
                    elif kind == "error":
                        print("   [error]", data.get("message"))
                    elif kind == "latency":
                        server.append(data["ms"])
                    elif kind == "agent_done":
                        gen = data.get("generation")
                        await replies.put((state["text"], state["bytes"], state["first_audio_at"]))
                        state.update(bytes=0, text="", first_audio_at=None)
                        await asyncio.sleep(0.3)   # "playback" finished
                        await ws.send_json({"type": "played", "generation": gen})

            async def mic_idle(stop: asyncio.Event):
                while not stop.is_set():
                    await ws.send_bytes(SILENCE)
                    await asyncio.sleep(0.02)

            task = asyncio.create_task(reader())
            text, size, _ = await asyncio.wait_for(replies.get(), 30)
            print(f"<< greeting: {text}  [{size} audio bytes = {size / 48000:.1f}s at 24 kHz]")
            for pcm in audio:
                await asyncio.sleep(0.8)
                await ws.send_json({"type": "caller_activity"})
                for i in range(0, len(pcm), FRAME):
                    await ws.send_bytes(pcm[i:i + FRAME].ljust(FRAME, b"\x00"))
                    await asyncio.sleep(0.02)
                stopped = time.monotonic()
                await ws.send_json({"type": "caller_speech_end"})
                stop = asyncio.Event()
                idle = asyncio.create_task(mic_idle(stop))
                text, size, first = await asyncio.wait_for(replies.get(), 45)
                stop.set()
                await idle
                if first:
                    heard.append(round((first - stopped) * 1000))
                delay = f"{(first - stopped) * 1000:.0f} ms" if first else "no audio"
                print(f"<< agent: {text}\n   [{size} audio bytes = {size / 48000:.1f}s; caller stop -> first audio {delay}]")
            await ws.send_json({"type": "finish"})
            await asyncio.sleep(1)
            task.cancel()
    return heard, server


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8935")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--tier", default="")
    parser.add_argument("--voice", default="")
    args = parser.parse_args()
    EXTRA = (f"&tier={args.tier}" if args.tier else "") + (f"&voice={args.voice}" if args.voice else "")
    asyncio.run(run_many(args.base, args.calls, args.out))
