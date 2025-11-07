# main.py - Adaptive Educational Video Generator v4.0
# FIXED: Videos now 30–90 sec (adaptive duration) | Works on Render

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess, tempfile, os, re, shutil, json, uuid
from datetime import datetime
import openai
import requests
from manim import *
import py_compile

# ============================
# CONFIG & KEYS
# ============================
OPENAI_API_KEY = "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"
ELEVEN_KEY = "sk_4fcbd8db995d809a60650ff2b2140e895815d5c28af93a19"

client = openai.OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OUTPUT_DIR = "/app/generated_videos"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================
# UTILS
# ============================
def run_cmd(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

def get_audio_duration(path):
    try:
        res = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path])
        return float(res.stdout.strip())
    except:
        return 0.0

def sanitize(text):
    if not text: return "Step"
    return re.sub(r'[^a-zA-Z0-9+\-=\(\)\[\]\{\}\/\*\.\,\s]', ' ', str(text)).strip()[:120]

# ============================
# TTS (ElevenLabs + Fallback)
# ============================
def tts_elevenlabs(text, out_path):
    url = "https://api.elevenlabs.io/v1/text-to-speech/pNInz6obpgDQGcFmaJgB"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
    data = {"text": text, "model_id": "eleven_turbo_v2"}
    r = requests.post(url, json=data, headers=headers, timeout=60)
    if r.status_code == 200:
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True
    return False

def generate_voice(text, out_path):
    if not tts_elevenlabs(text, out_path):
        # Fallback silence
        run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "3", out_path])

# ============================
# ADAPTIVE SCRIPT (GPT-4o)
# ============================
SCRIPT_PROMPT = """You are an expert math tutor. Generate a JSON script for an animated video.

RULES:
1. Duration: 30–90 seconds total
2. 4–12 segments
3. For solving: Use "calculation" layout with ALL steps in step_data
4. Narration: Natural, 20–50 words per segment
5. display_text: Clean, fits on screen
6. NO generic intros like "Introduction"

OUTPUT JSON:
{
  "title": "Solve x² - 5x + 6 = 0",
  "segments": [
    {
      "duration": 8,
      "narration": "Let's factor this quadratic...",
      "display_text": "(x + 2)(x + 3) = 0",
      "layout": "calculation",
      "step_data": {
        "calculation_steps": ["x² - 5x + 6 = 0", "(x - 2)(x - 3) = 0", "x = 2 or x = 3"],
        "annotations": ["Factor", "Set to zero", "Solutions"]
      }
    }
  ]
}
"""

def generate_script(topic):
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SCRIPT_PROMPT},
                {"role": "user", "content": f"Topic: {topic}"}
            ],
            response_format={"type": "json_object"},
            max_tokens=1500
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        return {
            "title": topic,
            "segments": [
                {"duration": 6, "narration": "Let's solve step by step", "display_text": topic, "layout": "title"}
            ]
        }

# ============================
# MANIM SCENE (Progressive Build)
# ============================
def generate_manim_code(segments):
    code = [
        "from manim import *\n",
        "class MathScene(Scene):\n",
        "    def construct(self):\n",
        "        self.camera.background_color = '#0a0a0a'\n",
        "        y = 3.0\n"
    ]

    for i, seg in enumerate(segments):
        dur = seg.get("duration", 6)
        layout = seg.get("layout", "text")
        text = sanitize(seg.get("display_text", ""))
        step_data = seg.get("step_data", {})

        if layout == "calculation" and step_data.get("calculation_steps"):
            steps = step_data["calculation_steps"]
            notes = step_data.get("annotations", [])
            per_step = dur / len(steps)

            for j, step in enumerate(steps):
                safe_step = sanitize(step)
                note = sanitize(notes[j]) if j < len(notes) else ""
                code.append(f"        # Step {j+1}\n")
                code.append(f"        eq{j} = MathTex(r\"{safe_step}\")\n")
                code.append(f"        eq{j}.shift(UP * y)\n")
                code.append(f"        self.play(Write(eq{j}), run_time={per_step*0.7})\n")
                if note:
                    code.append(f"        note{j} = Text(\"{note}\", font_size=24, color=YELLOW)\n")
                    code.append(f"        note{j}.next_to(eq{j}, RIGHT)\n")
                    code.append(f"        self.play(FadeIn(note{j}), run_time={per_step*0.3})\n")
                code.append(f"        y -= 0.8\n")
                code.append(f"        self.wait({per_step*0.3})\n")
        else:
            code.append(f"        txt{i} = Text(\"{text}\", font_size=36)\n")
            code.append(f"        txt{i}.shift(UP * y)\n")
            code.append(f"        self.play(Write(txt{i}), run_time={dur*0.7})\n")
            code.append(f"        y -= 0.8\n")
            code.append(f"        self.wait({dur*0.3})\n")

    code.append("        self.wait(2)\n")
    return "\n".join(code)

# ============================
# ENDPOINT
# ============================
@app.post("/generate")
async def generate(req: Request):
    data = await req.json()
    prompt = data.get("prompt", "").strip()
    quality = data.get("quality", "low")

    if not prompt:
        raise HTTPException(400, "prompt required")

    job_id = str(uuid.uuid4())
    tmpdir = tempfile.mkdtemp()
    outdir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(outdir, exist_ok=True)

    try:
        # 1. Script
        script = generate_script(prompt)
        segments = script.get("segments", [])
        with open(f"{outdir}/script.json", "w") as f:
            json.dump(script, f, indent=2)

        # 2. Audio
        audio_paths = []
        for i, seg in enumerate(segments):
            text = seg.get("narration", "Step")
            path = f"{tmpdir}/seg{i}.mp3"
            generate_voice(text, path)
            audio_paths.append(path)

        # Concat audio
        concat = f"{tmpdir}/list.txt"
        with open(concat, "w") as f:
            for p in audio_paths:
                f.write(f"file '{p}'\n")
        full_audio = f"{tmpdir}/full.mp3"
        run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat, "-c", "copy", full_audio])

        # 3. Manim
        scene_code = generate_manim_code(segments)
        scene_path = f"{tmpdir}/scene.py"
        with open(scene_path, "w") as f:
            f.write(scene_code)

        # Render
        config.media_dir = tmpdir
        config.output_file = f"{tmpdir}/video"
        config.pixel_width = 854 if quality == "low" else 1280
        config.pixel_height = 480 if quality == "low" else 720
        config.format = "mp4"
        config.verbosity = "WARNING"

        scene = type("DynamicScene", (Scene,), {"construct": lambda self: exec(scene_code)}())
        scene.render()

        # Find video
        video_path = None
        for f in os.listdir(f"{tmpdir}/media/videos/480p15" if quality == "low" else f"{tmpdir}/media/videos/720p30"):
            if f.endswith(".mp4"):
                video_path = f"{tmpdir}/media/videos/480p15/{f}" if quality == "low" else f"{tmpdir}/media/videos/720p30/{f}"
                break

        # 4. Merge
        final = f"{outdir}/final.mp4"
        run_cmd(["ffmpeg", "-y", "-i", video_path, "-i", full_audio, "-c:v", "copy", "-c:a", "aac", final])

        return FileResponse(final, media_type="video/mp4", filename=f"video_{job_id[:8]}.mp4")

    except Exception as e:
        with open(f"{outdir}/error.txt", "w") as f:
            f.write(str(e))
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ============================
# ROOT
# ============================
@app.get("/")
def root():
    return {
        "service": "Adaptive Educational Video Generator",
        "version": "4.0 - Fixed Duration",
        "status": "LIVE",
        "features": ["30–90 sec videos", "step-by-step", "voice", "Manim"],
        "url": "https://manimbackend-5.onrender.com"
    }
