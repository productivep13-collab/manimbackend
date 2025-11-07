# main.py - Complete Adaptive Educational Video Generator
# Enhanced with intelligent duration control and step-by-step visual animations

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess, tempfile, os, sys, re, shutil, json, time
from datetime import datetime
import py_compile
from pathlib import Path
import openai
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# ---------------------------
# Configuration
# ---------------------------
OPENAI_API_KEY = "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"
client = openai.OpenAI(api_key=OPENAI_API_KEY)
ELEVEN_KEY = "sk_4fcbd8db995d809a60650ff2b2140e895815d5c28af93a19"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_videos")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------
# Utilities
# ---------------------------
def cleanup_temp_dir(tmpdir: str):
    try:
        shutil.rmtree(tmpdir)
    except Exception:
        pass

def run_cmd(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

def get_audio_duration(audio_path: str) -> float:
    try:
        res = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", audio_path], timeout=15)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

def escape_text_safe(s: str) -> str:
    """Ultra-safe text escaping for Python strings"""
    if not s:
        return ""
    s = s.replace("\\", "")
    s = s.replace('"', "'")
    s = s.replace("\r", " ")
    s = s.replace("\n", " ")
    s = s.replace("\t", " ")
    s = re.sub(r'\s+', ' ', s)
    return s.strip()[:150]

# ---------------------------
# TTS Generation
# ---------------------------
def tts_elevenlabs(text: str, out_path: str):
    import requests
    VOICE_ID = "pNInz6obpgDQGcFmaJgB"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    data = {"text": text, "model_id": "eleven_turbo_v2"}
    r = requests.post(url, headers=headers, json=data, timeout=60)
    if r.status_code == 200:
        with open(out_path, "wb") as f:
            f.write(r.content)
    else:
        raise RuntimeError(f"TTS failed: {r.status_code}")

def tts_pyttsx3(text: str, out_path: str):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        tmpwav = out_path + ".wav"
        engine.save_to_file(text, tmpwav)
        engine.runAndWait()
        res = run_cmd(["ffmpeg", "-y", "-i", tmpwav, "-codec:a", "libmp3lame", "-qscale:a", "2", out_path], timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed")
        try:
            os.remove(tmpwav)
        except:
            pass
    except Exception as e:
        raise RuntimeError(f"pyttsx3 failed: {e}")

# Replace your generate_voice_audio_with_fallback function with this:

def generate_voice_audio_with_fallback(text: str, out_path: str, target_duration: float = None):
    """
    Generate TTS audio with proper fallback duration handling
    
    Args:
        text: Text to speak
        out_path: Output audio file path
        target_duration: Expected duration (used for fallback)
    """
    # Try ElevenLabs first
    if ELEVEN_KEY:
        try:
            tts_elevenlabs(text, out_path)
            actual_dur = get_audio_duration(out_path)
            if actual_dur > 0:
                print(f"[TTS] ElevenLabs: {actual_dur:.2f}s")
                return
            else:
                print(f"[TTS] ElevenLabs returned 0-length audio, trying fallback...")
        except Exception as e:
            print(f"[WARN] ElevenLabs failed: {e}")
    
    # Try pyttsx3
    try:
        tts_pyttsx3(text, out_path)
        actual_dur = get_audio_duration(out_path)
        if actual_dur > 0:
            print(f"[TTS] pyttsx3: {actual_dur:.2f}s")
            return
        else:
            print(f"[TTS] pyttsx3 returned 0-length audio, using silent fallback...")
    except Exception as e:
        print(f"[WARN] pyttsx3 failed: {e}")
    
    # Silent fallback - use target_duration or estimate from text
    if target_duration:
        fallback_dur = target_duration
    else:
        # Estimate: ~2.5 words per second, minimum 2s, maximum 10s
        word_count = len(text.split())
        fallback_dur = max(2.0, min(10.0, word_count / 2.5))
    
    print(f"[TTS] Silent fallback: {fallback_dur:.2f}s (text: {len(text)} chars, {len(text.split())} words)")
    
    run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
            "-t", f"{fallback_dur:.2f}", out_path], timeout=15)


# Also update the _tts_worker to pass target duration:

# Replace existing _tts_worker and generate_all_tts_parallel with this robust version

def _tts_worker(args):
    """
    args may be either:
      (narration, outpath)
    or
      (narration, outpath, target_duration)
    This worker is tolerant to both forms.
    """
    # Defensive unpacking to avoid ValueError
    try:
        if isinstance(args, (list, tuple)) and len(args) == 3:
            narration, outpath, target_duration = args
        elif isinstance(args, (list, tuple)) and len(args) == 2:
            narration, outpath = args
            target_duration = None
        else:
            # Unexpected shape — try best effort
            narration = args[0]
            outpath = args[1]
            target_duration = None
    except Exception as e:
        # Return an error tuple so caller can handle it
        return (None, RuntimeError(f"Bad args passed to _tts_worker: {args} ({e})"))

    try:
        # Generate TTS (function does not currently accept target_duration)
        generate_voice_audio_with_fallback(narration, outpath)
        return (outpath, None)
    except Exception as e:
        # Return path + exception for the caller to inspect/handle
        return (outpath, e)


def generate_all_tts_parallel(segments, tmpdir, max_workers=6):
    """Generate TTS for all segments in parallel (robust to task tuple shapes)."""
    tasks = []
    for i, seg in enumerate(segments):
        narration = seg.get("narration", "").strip() or f"Segment {i+1}"
        outp = os.path.join(tmpdir, f"segment_{i:03d}.mp3")

        # Optionally include target duration for future padding logic:
        target_dur = None
        try:
            # Prefer the normalized duration if present
            target_dur = float(seg.get("duration", seg.get("estimated_duration", 0))) if seg else None
        except Exception:
            target_dur = None

        # Use 2-tuple for compatibility; if you later want padding inside the worker, change to 3-tuple.
        tasks.append((narration, outp))
        # If you prefer to pass target duration now, uncomment the next line and comment the 2-tuple above:
        # tasks.append((narration, outp, target_dur))

    audio_paths = [None] * len(tasks)
    workers = min(max_workers, max(1, len(tasks)))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_index = {ex.submit(_tts_worker, t): idx for idx, t in enumerate(tasks)}

        for fut in as_completed(future_to_index):
            idx = future_to_index[fut]
            outp, err = fut.result()

            if outp is None:
                # Worker returned an unexpected value; create a silent fallback for stability
                print(f"[WARN] TTS worker returned no output for segment {idx+1}. Using silent fallback.")
                outp = os.path.join(tmpdir, f"segment_{idx:03d}.mp3")
                run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                         "-t", "3.0", outp], timeout=15)
                err = None

            if err:
                print(f"[WARN] TTS failed for segment {idx+1}: {err}")
                # Create a short silent fallback equal to segment.duration (or 3s min)
                fallback_dur = max(3.0, float(segments[idx].get("duration", 3.0)))
                run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                         "-t", f"{fallback_dur:.2f}", outp], timeout=15)

            actual = get_audio_duration(outp)
            if actual <= 0:
                actual = float(segments[idx].get("duration", 4.0))

            segments[idx]["actual_duration"] = actual
            segments[idx]["audio_path"] = outp
            audio_paths[idx] = outp

    return audio_paths

# ---------------------------
# ADAPTIVE Script Generation
# ---------------------------
SCRIPT_PROMPT = """You are an expert educational content creator with adaptive pacing.

CRITICAL: Analyze the topic complexity and generate APPROPRIATE duration:
- Simple calculations (solve 2x+5=9): 20-40 seconds, 3-5 segments
- Single concept explanation (Pythagoras theorem): 60-90 seconds, 6-10 segments  
- Complex derivations (quadratic formula derivation): 2-3 minutes, 12-18 segments
- Comprehensive topics (calculus chapter): 4-6 minutes, 20-30 segments

OUTPUT: JSON format only:
{
  "title": "Clear, engaging title",
  "complexity": "simple|moderate|complex|comprehensive",
  "estimated_duration": 45,
  "segments": [
    {
      "segment_id": 1,
      "duration": 6,
      "narration": "Natural explanation (20-60 words based on complexity)",
      "display_text": "What appears on screen",
      "layout": "title|calculation|step|equation|split|diagram|example",
      "visual_type": "write|solve|graph|geometry|formula|animate",
      "animation_style": "write_step_by_step|fade_in|transform|highlight",
      "step_data": {
        "calculation_steps": ["2(x-9)", "2x-18", "2x-18+12x", "14x-18"],
        "annotations": ["Distribute", "Combine terms", "Add", "Divide"],
        "highlight_parts": ["x", "coefficient"]
      },
      "color_scheme": "blue|purple|green|orange"
    }
  ]
}

LAYOUT TYPES & WHEN TO USE:
1. "title" - Only for video intro (1 segment)
2. "calculation" - Step-by-step math solving with visual writing
3. "step" - Single derivation/proof step with highlighting
4. "equation" - Display important formulas
5. "split" - Concept + visual diagram side-by-side
6. "diagram" - Geometric or graphical representation
7. "example" - Full worked problem

ANIMATION STYLES:
1. "write_step_by_step" - Write calculations as if on board
2. "transform" - Morph one expression into another
3. "highlight" - Emphasize specific parts
4. "fade_in" - Gentle appearance
5. "build" - Add elements progressively

CRITICAL RULES:
1. **Duration Judgment**:
   - "2+2=4" → 15 seconds (2 segments)
   - "Solve 2(x-9)+3x(4)=99" → 40 seconds (5-6 segments showing each step)
   - "Derive quadratic formula" → 2-3 minutes (15-20 segments)
   
2. **Visual Calculations**:
   - ALWAYS show math being written step-by-step on screen
   - For "solve X", create segments for EACH calculation step
   - Use "calculation" layout with step_data containing each line
   
3. **No Generic Text**:
   - NEVER: "Derivationof y=mx+c", "Introduction1", "Step2"
   - ALWAYS: "Let's derive the slope-intercept form", "Starting with two points"
   
4. **Segment Distribution Examples**:
   
   Simple (30s): "Expand 2(x+3)"
   - Segment 1 (5s): Title - "Let's Expand This Expression"
   - Segment 2 (8s): Show original: 2(x+3)
   - Segment 3 (8s): Apply distributive: 2×x + 2×3
   - Segment 4 (6s): Simplify: 2x + 6
   - Segment 5 (3s): Final answer highlight
   
   Moderate (90s): "Solve quadratic: x²+5x+6=0"
   - Segment 1 (5s): Title + hook
   - Segment 2 (8s): Identify a,b,c values
   - Segment 3 (10s): Factor approach: (x+2)(x+3)
   - Segment 4 (8s): Set each factor to zero
   - Segment 5 (8s): Solve x+2=0 → x=-2
   - Segment 6 (8s): Solve x+3=0 → x=-3
   - Segment 7 (7s): Verify solutions
   - Segment 8 (6s): Graph visualization
   
   Complex (3-4m): "Derive quadratic formula"
   - Title + motivation (2 segments)
   - Start with ax²+bx+c=0 (2 segments)
   - Divide by a (2 segments)
   - Complete the square steps (6-8 segments)
   - Simplify square root (3 segments)
   - Final formula (2 segments)
   - Example application (4 segments)

5. **step_data Structure**:
   For calculations, provide the exact steps:
   {
     "calculation_steps": [
       "2(x-9) + 3x(4) = 99",
       "2x - 18 + 12x = 99",
       "14x - 18 = 99",
       "14x = 117",
       "x = 8.36"
     ],
     "annotations": ["Distribute", "Combine like terms", "Add 18", "Divide by 14"]
   }

6. **Narration Guidelines**:
   - Simple topics: 15-30 words per segment
   - Moderate: 30-50 words
   - Complex: 40-70 words
   - Always natural, conversational tone
   - Explain WHY, not just WHAT

7. **No Filler**:
   - NO "subscribe", "like", "watch more"
   - NO unnecessary repetition
   - Pure educational content only

8. **Engaging Intros**:
   - "Ever wondered why this formula works?"
   - "Let's solve this step by step"
   - "Here's a clever trick for..."
   NOT: "Introduction to X", "Topic: X", "Derivation1"

9. **Text Rendering**:
   - Keep display_text under 80 characters
   - Use proper spacing
   - Break long equations into multiple segments
   - Ensure text fits on 1920x1080 screen

10. **Adaptive Complexity**:
    - Detect if topic is procedural (solving) vs conceptual (understanding)
    - Procedural: More calculation segments
    - Conceptual: More explanation + diagram segments

Analyze the user's query and determine the appropriate scope before generating segments.
# Add to SCRIPT_PROMPT
For calculation-heavy topics:
- Use ONE "calculation" segment with ALL steps in step_data
- Don't split into multiple segments - keep the flow continuous
- Example: "Solve 2x+5=9" should be ONE segment with all steps, not 5 segments
"""

def generate_script_with_gpt4_adaptive(topic: str) -> dict:
    """Generate adaptive script based on topic complexity"""
    
    # Phase 1: Analyze complexity
    analysis_prompt = f"""Analyze this topic and determine complexity:
Topic: {topic}

Respond with JSON:
{{
  "complexity": "simple|moderate|complex|comprehensive",
  "reasoning": "why this complexity",
  "recommended_duration": 30,
  "recommended_segments": 5,
  "is_procedural": true,
  "key_concepts": ["list", "of", "concepts"]
}}

Complexity Guidelines:
- Simple: Basic calculations, single-step problems (20-60s, 3-8 segments)
- Moderate: Multi-step problems, single concept explanation (60-120s, 8-12 segments)
- Complex: Derivations, proofs, multiple concepts (2-4 minutes, 12-20 segments)
- Comprehensive: Full topic coverage, multiple examples (4-6 minutes, 20-30 segments)

Is Procedural: Does this involve step-by-step solving/calculation?
"""
    
    print(f"[ANALYZE] Topic: {topic}")
    
    try:
        # Get complexity analysis
        analysis_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.7,
            max_completion_tokens=500,
            response_format={"type": "json_object"}
        )
        
        analysis = json.loads(analysis_response.choices[0].message.content)
        complexity = analysis.get("complexity", "moderate")
        recommended_duration = analysis.get("recommended_duration", 60)
        recommended_segments = analysis.get("recommended_segments", 8)
        is_procedural = analysis.get("is_procedural", False)
        
        print(f"[ANALYZE] Complexity: {complexity}")
        print(f"[ANALYZE] Procedural: {is_procedural}")
        print(f"[ANALYZE] Target: {recommended_duration}s, {recommended_segments} segments")
        
        # Phase 2: Generate script with complexity-aware instructions
        script_messages = [
            {"role": "system", "content": SCRIPT_PROMPT},
            {"role": "user", "content": f"""Create educational video for: {topic}

Complexity Analysis:
- Level: {complexity}
- Target duration: {recommended_duration} seconds
- Target segments: {recommended_segments}
- Procedural (step-by-step solving): {is_procedural}

CRITICAL INSTRUCTIONS:
1. Match duration to complexity - DO NOT over-explain simple topics
2. For calculations/solving: Use "calculation" layout with step_data showing EVERY step
3. Show math being written step-by-step on screen
4. NO generic intros like "Introduction to X" or "Derivation1"
5. Use natural, engaging narration
6. Each segment should have proper display_text that fits on screen

{'IMPORTANT: This is a PROCEDURAL topic. Show step-by-step calculations visually with calculation layout.' if is_procedural else 'IMPORTANT: This is a CONCEPTUAL topic. Focus on understanding with diagrams and explanations.'}

Generate complete, detailed script now."""}
        ]
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=script_messages,
            temperature=0.7,
            max_completion_tokens=4000,
            response_format={"type": "json_object"}
        )
        
        script_data = json.loads(response.choices[0].message.content)
        
        # Validate
        if "segments" not in script_data:
            raise ValueError("No segments in script")
        
        if len(script_data["segments"]) < 2:
            raise ValueError("Too few segments")
        
        # Add analysis metadata
        script_data["complexity"] = complexity
        script_data["analysis"] = analysis
        script_data["is_procedural"] = is_procedural
        
        print(f"[SCRIPT] Generated {len(script_data['segments'])} segments")
        total_time = sum(s.get("duration", 5) for s in script_data["segments"])
        print(f"[SCRIPT] Total duration: {total_time}s ({total_time/60:.1f} minutes)")
        
        return script_data
        
    except Exception as e:
        print(f"[ERROR] Script generation failed: {e}")
        traceback.print_exc()
        
        # Minimal fallback
        return {
            "title": topic,
            "complexity": "simple",
            "segments": [
                {
                    "segment_id": 1,
                    "duration": 4,
                    "narration": f"Let's explore {topic} step by step",
                    "display_text": topic,
                    "layout": "title",
                    "animation_style": "fade_in",
                    "color_scheme": "blue"
                },
                {
                    "segment_id": 2,
                    "duration": 8,
                    "narration": "We'll break this down into clear, manageable steps to build complete understanding",
                    "display_text": "Step-by-Step Approach",
                    "layout": "split",
                    "animation_style": "write_step_by_step",
                    "color_scheme": "purple"
                }
            ]
        }

# ---------------------------
# ADAPTIVE MANIM Scene Generator
# ---------------------------
# Key fixes for the Manim scene generation
# Replace the generate_manim_scene_adaptive function with this improved version

# COMPLETE FIXED VERSION - Replace generate_manim_scene_adaptive function in your main.py

def sanitize_for_text(text: str) -> str:
    """Ultra-safe text cleaning for Manim Text objects"""
    if not text:
        return "Empty"
    
    # Remove all problematic characters
    text = str(text)
    text = text.replace("\\", " ")
    text = text.replace('"', "'")
    text = text.replace("\r", " ")
    text = text.replace("\n", " ")
    text = text.replace("\t", " ")
    text = re.sub(r'\s+', ' ', text)
    
    # Keep only safe characters
    safe_chars = []
    for char in text:
        if char.isprintable() and ord(char) < 128:
            safe_chars.append(char)
        else:
            safe_chars.append(' ')
    
    result = ''.join(safe_chars).strip()
    return result[:120] if result else "Content"


def generate_manim_scene_adaptive(segments: list) -> str:
    """Generate Manim scenes that MATCH audio durations exactly"""
    
    scene_code = ["""from manim import *
import numpy as np

class GeneratedScene(Scene):
    def construct(self):
        self.camera.background_color = "#0a0a0a"
        self.current_y = 3.0
        self.line_height = 0.7
        
"""]
    
    for i, seg in enumerate(segments):
        segment_id = i + 1
        # CRITICAL: Use actual audio duration
        duration = float(seg.get("actual_duration", seg.get("duration", 5.0)))
        layout = seg.get("layout", "split")
        step_data = seg.get("step_data", {})
        
        display_text = sanitize_for_text(seg.get("display_text", ""))
        color_scheme = seg.get("color_scheme", "blue")
        
        color_map = {
            "blue": "#3b82f6", "purple": "#a855f7", "green": "#10b981",
            "orange": "#f97316", "red": "#ef4444", "yellow": "#eab308"
        }
        color = color_map.get(color_scheme, "#3b82f6")
        
        scene_code.append(f"""
        # === SEGMENT {segment_id}: {layout.upper()} (Duration: {duration:.2f}s) ===
""")
        
        # Clear screen for new title sections
        if layout == "title" and i > 0:
            scene_code.append("""
        self.play(*[FadeOut(m) for m in self.mobjects], run_time=0.5)
        self.current_y = 3.0
        self.wait(0.2)
""")
        
        # === CALCULATION: Match audio duration ===
        if layout == "calculation" and step_data.get("calculation_steps"):
            steps = step_data["calculation_steps"]
            notes = step_data.get("annotations", [])
            
            # Distribute duration across steps
            num_steps = len(steps)
            time_per_step = duration / max(num_steps, 1)
            animation_time = time_per_step * 0.7
            wait_time = time_per_step * 0.3
            
            scene_code.append(f"""
        # Calculation with {num_steps} steps over {duration:.1f}s
        
""")
            
            for j, step_text in enumerate(steps):
                safe_step = sanitize_for_text(step_text)
                safe_note = sanitize_for_text(notes[j]) if j < len(notes) else ""
                
                scene_code.append(f"""
        step_{j} = Text("{safe_step}", font_size=32, color=WHITE)
        step_{j}.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5)
""")
                
                if safe_note:
                    scene_code.append(f"""
        note_{j} = Text("{safe_note}", font_size=22, color="{color}", slant=ITALIC)
        note_{j}.next_to(step_{j}, RIGHT, buff=0.7)
        self.play(Write(step_{j}), FadeIn(note_{j}, shift=RIGHT*0.2), run_time={animation_time:.2f})
""")
                else:
                    scene_code.append(f"""
        self.play(Write(step_{j}), run_time={animation_time:.2f})
""")
                
                scene_code.append(f"""
        self.wait({wait_time:.2f})
        self.current_y -= self.line_height
""")
            
            # Highlight answer
            scene_code.append(f"""
        answer_box = SurroundingRectangle(step_{len(steps)-1}, color="{color}", 
                                         buff=0.15, stroke_width=3, corner_radius=0.1)
        self.play(Create(answer_box), run_time=0.5)
        self.wait(0.5)
""")
        
        # === STEP ===
        elif layout == "step":
            anim_time = duration * 0.6
            wait_time = duration * 0.4
            
            scene_code.append(f"""
        label = Text("Step {segment_id}", font_size=24, color="{color}", weight=BOLD)
        label.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5.5)
        
        content = Text("{display_text}", font_size=30, color=WHITE)
        content.next_to(label, DOWN, buff=0.25, aligned_edge=LEFT)
        
        self.play(FadeIn(label, shift=DOWN*0.15), run_time=0.3)
        self.play(Write(content), run_time={anim_time:.2f})
        self.wait({wait_time:.2f})
        
        self.current_y -= self.line_height * 1.8
""")
        
        # === TITLE ===
        elif layout == "title":
            if i == 0:
                anim_time = duration * 0.5
                wait_time = duration * 0.3
                
                scene_code.append(f"""
        title = Text("{display_text}", font_size=52, weight=BOLD, color=WHITE)
        title.move_to(ORIGIN)
        
        line = Line(LEFT*4.5, RIGHT*4.5, color="{color}", stroke_width=4)
        line.next_to(title, DOWN, buff=0.35)
        
        self.play(Write(title), run_time={anim_time:.2f})
        self.play(Create(line), run_time=0.5)
        self.wait({wait_time:.2f})
        
        self.play(FadeOut(title, shift=UP*0.5), FadeOut(line, shift=UP*0.5), run_time=0.6)
        self.current_y = 3.0
""")
            else:
                anim_time = duration * 0.4
                wait_time = duration * 0.6
                
                scene_code.append(f"""
        header = Text("{display_text}", font_size=34, weight=BOLD, color="{color}")
        header.to_edge(UP, buff=0.6)
        
        line = Line(LEFT*6, RIGHT*6, color="{color}", stroke_width=2)
        line.next_to(header, DOWN, buff=0.2)
        
        self.play(Write(header), Create(line), run_time={anim_time:.2f})
        self.current_y = 2.2
        self.wait({wait_time:.2f})
""")
        
        # === EQUATION ===
        elif layout == "equation":
            anim_time = duration * 0.7
            wait_time = duration * 0.3
            
            scene_code.append(f"""
        label = Text("Formula:", font_size=22, color="{color}")
        label.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5.5)
        
        eq = Text("{display_text}", font_size=36, color=WHITE)
        eq.next_to(label, RIGHT, buff=0.6)
        
        box = SurroundingRectangle(eq, color="{color}", buff=0.25, 
                                   stroke_width=2, corner_radius=0.12)
        box.set_fill("{color}", opacity=0.08)
        
        self.play(Write(label), run_time=0.3)
        self.play(Create(box), Write(eq), run_time={anim_time:.2f})
        self.wait({wait_time:.2f})
        
        self.current_y -= 1.0
""")
        
        # === DIAGRAM ===
        elif layout == "diagram":
            anim_time = duration * 0.6
            wait_time = duration * 0.4
            
            scene_code.append(f"""
        label = Text("{display_text[:50]}", font_size=26, color=WHITE)
        label.move_to([3.5, self.current_y, 0])
        
        shape = Circle(radius=0.8, color="{color}", stroke_width=2, fill_opacity=0.1)
        shape.move_to([3.5, self.current_y-1.2, 0])
        
        self.play(Write(label), run_time={anim_time*0.4:.2f})
        self.play(Create(shape), run_time={anim_time*0.6:.2f})
        self.wait({wait_time:.2f})
""")
        
        # === SPLIT ===
        elif layout == "split":
            anim_time = duration * 0.7
            wait_time = duration * 0.3
            
            scene_code.append(f"""
        text = Text("{display_text[:60]}", font_size=26, color=WHITE)
        text.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5)
        text.scale_to_fit_width(5)
        
        circles = VGroup(*[
            Circle(radius=0.2+i*0.08, color="{color}", stroke_width=2, fill_opacity=0.05)
            for i in range(3)
        ])
        circles.move_to([4, self.current_y, 0])
        
        self.play(FadeIn(text, shift=RIGHT*0.2), Create(circles), run_time={anim_time:.2f})
        self.wait({wait_time:.2f})
        self.current_y -= 1.2
""")
        
        # === EXAMPLE ===
        elif layout == "example":
            anim_time = duration * 0.6
            wait_time = duration * 0.4
            
            scene_code.append(f"""
        ex_label = Text("Example:", font_size=26, color="{color}", weight=BOLD)
        ex_label.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5.5)
        
        ex_text = Text("{display_text[:70]}", font_size=24, color=WHITE)
        ex_text.next_to(ex_label, DOWN, buff=0.3, aligned_edge=LEFT)
        ex_text.scale_to_fit_width(10)
        
        self.play(Write(ex_label), run_time={anim_time*0.3:.2f})
        self.play(FadeIn(ex_text, shift=DOWN*0.2), run_time={anim_time*0.7:.2f})
        self.wait({wait_time:.2f})
        
        self.current_y -= 1.4
""")
        
        # === GENERIC FALLBACK ===
        else:
            anim_time = duration * 0.7
            wait_time = duration * 0.3
            
            scene_code.append(f"""
        text = Text("{display_text}", font_size=30, color=WHITE)
        text.move_to([0, self.current_y, 0], aligned_edge=LEFT).shift(LEFT*5)
        text.scale_to_fit_width(11)
        
        self.play(Write(text), run_time={anim_time:.2f})
        self.wait({wait_time:.2f})
        self.current_y -= 0.9
""")
        
        # Auto-fade old content periodically
        if segment_id > 0 and segment_id % 7 == 0:
            scene_code.append("""
        if self.current_y < -2.5:
            old_mobs = self.mobjects[:-2] if len(self.mobjects) > 2 else []
            if old_mobs:
                self.play(*[mob.animate.set_opacity(0.2) for mob in old_mobs], run_time=0.4)
            self.current_y = 2.5
""")
    
    # Ending - hold for 2 seconds
    scene_code.append("""
        # Final hold
        self.wait(2.0)
        self.play(*[FadeOut(m) for m in self.mobjects], run_time=1.2)
""")
    
    return "\n".join(scene_code)
  

def validate_and_fix_scene(scene_path: str, outdir: str) -> bool:
    """Validate Manim scene syntax"""
    try:
        py_compile.compile(scene_path, doraise=True)
        print("[VALIDATE] ✓ Scene syntax valid")
        return True
    except py_compile.PyCompileError as e:
        error_msg = str(e)
        print(f"[VALIDATE] ✗ Syntax error: {error_msg[:200]}")
        
        with open(os.path.join(outdir, "validation_error.txt"), "w") as f:
            f.write(f"Validation Error:\n{error_msg}\n\n")
            f.write(f"Scene file: {scene_path}\n")
        
        return False

# ---------------------------
# Manim Execution
# ---------------------------
def run_manim_with_logging(scene_path: str, quality: str, tmpdir: str, logfile: str):
    """Run Manim with logging"""
    quality_map = {
        "low": "-ql",
        "medium": "-qm",
        "high": "-qh"
    }
    quality_flag = quality_map.get(quality, "-ql")
    
    cmd = [
        sys.executable, "-m", "manim",
        quality_flag,
        "--format", "mp4",
        "--disable_caching",
        "--output_file", "output",
        scene_path,
        "GeneratedScene"
    ]
    
    print(f"[MANIM] Running: {' '.join(cmd[:5])}...")
    
    with open(logfile, "w", encoding="utf-8") as log:
        log.write(f"Command: {' '.join(cmd)}\n")
        log.write(f"Working dir: {tmpdir}\n")
        log.write(f"Time: {datetime.now()}\n")
        log.write("="*70 + "\n\n")
        
        process = subprocess.Popen(
            cmd,
            cwd=tmpdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        for line in process.stdout:
            log.write(line)
            if "ERROR" in line or "CRITICAL" in line or "Traceback" in line:
                print(f"[MANIM ERROR] {line.strip()}")
        
        process.wait()
        
        log.write(f"\n\nReturn code: {process.returncode}\n")
        
        return process.returncode

# ---------------------------
# Main Generation Endpoint
# ---------------------------
@app.post("/generate")
async def generate(background_tasks: BackgroundTasks, req: Request):
    data = await req.json()
    prompt = (data.get("prompt") or "").strip()
    quality = data.get("quality", "low")
    
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    
    print("\n" + "="*70)
    print(f"[START] Topic: {prompt}")
    print(f"[START] Quality: {quality}")
    print(f"[START] Time: {datetime.now().strftime('%H:%M:%S')}")
    
    tmpdir = tempfile.mkdtemp(prefix="vidgen_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(OUTPUT_DIR, timestamp)
    os.makedirs(outdir, exist_ok=True)
    
    with open(os.path.join(outdir, "request.json"), "w") as f:
        json.dump(data, f, indent=2)
    
    try:
        # STEP 1: Generate adaptive script
        print("\n[1/6] Generating adaptive script...")
        script_data = generate_script_with_gpt4_adaptive(prompt)
        segments = script_data.get("segments", [])
        
        with open(os.path.join(outdir, "script.json"), "w", encoding="utf-8") as f:
            json.dump(script_data, f, indent=2, ensure_ascii=False)
        
        complexity = script_data.get("complexity", "moderate")
        print(f"[1/6] ✓ Generated {len(segments)} segments (complexity: {complexity})")
        
         # STEP 2: Generate audio in parallel
        print("\n[2/6] Generating audio...")
        audio_paths = generate_all_tts_parallel(segments, tmpdir, max_workers=4)
        
        # === CRITICAL DEBUG: Check actual durations ===
        print("\n" + "="*70)
        print("DURATION ANALYSIS:")
        print("="*70)
        
        for i, seg in enumerate(segments):
            planned = seg.get("duration", 0)
            actual = seg.get("actual_duration", 0)
            narration = seg.get("narration", "")[:50]
            layout = seg.get("layout", "unknown")
            
            print(f"Segment {i+1:2d} ({layout:12s}): "
                  f"Planned={planned:5.2f}s, Actual={actual:5.2f}s | {narration}...")
        
        total_planned = sum(seg.get("duration", 5.0) for seg in segments)
        total_actual = sum(seg.get("actual_duration", 5.0) for seg in segments)
        
        print("="*70)
        print(f"TOTALS: Planned={total_planned:.1f}s ({total_planned/60:.1f}min), "
              f"Actual={total_actual:.1f}s ({total_actual/60:.1f}min)")
        print("="*70 + "\n")
        
        if total_actual < 15:
            print("⚠️  WARNING: Total audio is very short! This will cause short videos.")
            print("⚠️  Check if TTS is actually generating audio or using fallback silent audio.")
        
        total_duration = total_actual
        print(f"[2/6] ✓ Audio ready: {total_duration:.1f}s total")
        
        # STEP 3: Concatenate audio
        print("\n[3/6] Concatenating audio...")
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in audio_paths:
                f.write(f"file '{p.replace(chr(92), '/')}'\n")
        
        final_audio = os.path.join(tmpdir, "voice.mp3")
        res = run_cmd([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libmp3lame", "-q:a", "2", final_audio
        ], timeout=120)
        
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail="Audio concatenation failed")
        
        shutil.copy2(final_audio, os.path.join(outdir, "audio.mp3"))
        print(f"[3/6] ✓ Audio concatenated")
        
        # STEP 4: Generate adaptive Manim code
        print("\n[4/6] Generating adaptive Manim scene...")
        scene_code = generate_manim_scene_adaptive(segments)
        
        scene_path = os.path.join(tmpdir, "scene.py")
        with open(scene_path, "w", encoding="utf-8") as f:
            f.write(scene_code)
        
        # Save copy for debugging
        shutil.copy2(scene_path, os.path.join(outdir, "scene.py"))
        
        # Validate
        if not validate_and_fix_scene(scene_path, outdir):
            raise HTTPException(status_code=500,
                              detail="Scene validation failed. Check validation_error.txt")
        
        print(f"[4/6] ✓ Adaptive scene validated")
        
        # STEP 5: Render with Manim
        print("\n[5/6] Rendering video with Manim...")
        manim_log = os.path.join(outdir, "manim_render.log")
        
        returncode = run_manim_with_logging(scene_path, quality, tmpdir, manim_log)
        
        if returncode != 0:
            with open(manim_log, "r", encoding="utf-8") as f:
                log_content = f.read()
            
            error_lines = [line for line in log_content.split("\n")
                          if "ERROR" in line or "Traceback" in line or "TypeError" in line]
            error_summary = "\n".join(error_lines[-10:]) if error_lines else "Unknown error"
            
            raise HTTPException(
                status_code=500,
                detail=f"Manim render failed:\n{error_summary}\n\nFull log: {manim_log}"
            )
        
        # Find rendered video
        video_path = None
        media_dir = os.path.join(tmpdir, "media")
        
        for root, dirs, files in os.walk(media_dir):
            for fname in files:
                if fname.endswith(".mp4"):
                    video_path = os.path.join(root, fname)
                    break
            if video_path:
                break
        
        if not video_path or not os.path.exists(video_path):
            raise HTTPException(
                status_code=500,
                detail=f"Rendered video not found in {media_dir}"
            )
        if video_path:
            video_duration = get_audio_duration(video_path)
            audio_duration = get_audio_duration(final_audio)
            
            print("\n" + "="*70)
            print("VIDEO vs AUDIO DURATION:")
            print("="*70)
            print(f"Manim video duration:  {video_duration:.2f}s ({video_duration/60:.1f}min)")
            print(f"Audio track duration:  {audio_duration:.2f}s ({audio_duration/60:.1f}min)")
            print(f"Expected total:        {total_duration:.2f}s ({total_duration/60:.1f}min)")
            
            if video_duration < audio_duration * 0.5:
                print("⚠️  CRITICAL: Manim video is MUCH shorter than audio!")
                print("⚠️  Manim animations are not respecting segment durations.")
                print("⚠️  Check the generated scene.py file.")
            elif video_duration < audio_duration * 0.9:
                print("⚠️  WARNING: Manim video is shorter than audio")
            else:
                print("✓ Video duration looks correct")
            
            print("="*70 + "\n")
        
        shutil.copy2(video_path, os.path.join(outdir, "video_only.mp4"))
        print(f"[5/6] ✓ Video rendered: {os.path.basename(video_path)}")

        
        shutil.copy2(video_path, os.path.join(outdir, "video_only.mp4"))
        print(f"[5/6] ✓ Video rendered: {os.path.basename(video_path)}")
        
        # STEP 6: Merge audio + video
        print("\n[6/6] Merging audio and video...")
        final_out = os.path.join(outdir, "final_output.mp4")
        
        merge_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", final_audio,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-map", "0:v:0",  # Use video from first input
            "-map", "1:a:0",
            final_out
        ]
        
        merge_res = run_cmd(merge_cmd, timeout=120)
        if merge_res.returncode != 0:
            raise HTTPException(status_code=500, detail="Audio/Video merge failed")
        
        print(f"\n{'='*70}")
        print(f"[SUCCESS] Video generated!")
        print(f"[SUCCESS] File: {final_out}")
        print(f"[SUCCESS] Duration: {total_duration:.1f}s ({total_duration/60:.1f} min)")
        print(f"[SUCCESS] Segments: {len(segments)}")
        print(f"[SUCCESS] Complexity: {complexity}")
        print(f"[SUCCESS] Output folder: {outdir}")
        print(f"{'='*70}\n")
        
        background_tasks.add_task(cleanup_temp_dir, tmpdir)
        
        return FileResponse(
            final_out,
            media_type="video/mp4",
            filename=f"video_{timestamp}.mp4"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"\n[CRITICAL ERROR] {type(e).__name__}: {e}")
        print(f"[DEBUG] Temp dir preserved: {tmpdir}")
        print(f"[DEBUG] Output dir: {outdir}")
        traceback.print_exc()
        
        with open(os.path.join(outdir, "error.txt"), "w") as f:
            f.write(f"Error: {type(e).__name__}\n")
            f.write(f"Message: {str(e)}\n\n")
            f.write(traceback.format_exc())
        
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {str(e)}\nCheck {outdir}/error.txt"
        )




# ---------------------------
# Info Endpoints
# ---------------------------
@app.get("/")
async def root():
    """API info and recent videos"""
    videos = []
    if os.path.exists(OUTPUT_DIR):
        for folder in sorted(os.listdir(OUTPUT_DIR), reverse=True)[:10]:
            folder_path = os.path.join(OUTPUT_DIR, folder)
            if os.path.isdir(folder_path):
                final_video = os.path.join(folder_path, "final_output.mp4")
                if os.path.exists(final_video):
                    size_mb = os.path.getsize(final_video) / (1024 * 1024)
                    videos.append({
                        "timestamp": folder,
                        "size_mb": round(size_mb, 2),
                        "url": f"/video/{folder}"
                    })
    
    return {
        "service": "Adaptive Educational Video Generator",
        "version": "4.0 - Intelligent Duration & Step-by-Step Edition",
        "model": "gpt-4o",
        "status": "operational",
        "features": [
            "Adaptive duration based on complexity",
            "Step-by-step visual calculations",
            "Procedural vs conceptual detection",
            "Natural engaging narration",
            "Professional Manim animations",
            "Parallel audio generation"
        ],
        "output_folder": OUTPUT_DIR,
        "recent_videos": videos
    }

@app.get("/video/{timestamp}")
async def get_video(timestamp: str):
    """Download specific video"""
    video_path = os.path.join(OUTPUT_DIR, timestamp, "final_output.mp4")
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video_path, media_type="video/mp4")

@app.get("/debug/{timestamp}")
async def debug_info(timestamp: str):
    """Get debug info for a generation"""
    folder_path = os.path.join(OUTPUT_DIR, timestamp)
    if not os.path.isdir(folder_path):
        raise HTTPException(status_code=404, detail="Generation not found")
    
    files = {}
    for fname in ["script.json", "scene.py", "manim_render.log", "error.txt", 
                  "validation_error.txt", "request.json"]:
        fpath = os.path.join(folder_path, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Truncate large files
                    if len(content) > 50000:
                        content = content[:50000] + "\n\n... [truncated]"
                    files[fname] = content
            except:
                files[fname] = "[binary or unreadable]"
    
    return JSONResponse(files)

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "python": sys.version,
        "output_dir": OUTPUT_DIR,
        "features": "adaptive_duration,step_by_step,procedural_detection"
    }

@app.get("/diagnose/{timestamp}")
async def diagnose_video(timestamp: str):
    """Detailed diagnostics for a video generation"""
    folder_path = os.path.join(OUTPUT_DIR, timestamp)
    if not os.path.isdir(folder_path):
        raise HTTPException(status_code=404, detail="Generation not found")
    
    diagnostics = {
        "timestamp": timestamp,
        "folder": folder_path
    }
    
    # Load script
    script_path = os.path.join(folder_path, "script.json")
    if os.path.exists(script_path):
        with open(script_path, "r") as f:
            script_data = json.load(f)
            segments = script_data.get("segments", [])
            
            diagnostics["num_segments"] = len(segments)
            diagnostics["planned_duration"] = sum(s.get("duration", 0) for s in segments)
            diagnostics["complexity"] = script_data.get("complexity", "unknown")
            
            diagnostics["segment_breakdown"] = [
                {
                    "id": i+1,
                    "layout": s.get("layout"),
                    "planned_duration": s.get("duration"),
                    "actual_duration": s.get("actual_duration"),
                    "narration_length": len(s.get("narration", ""))
                }
                for i, s in enumerate(segments)
            ]
    
    # Check file durations
    for filename in ["audio.mp3", "video_only.mp4", "final_output.mp4"]:
        filepath = os.path.join(folder_path, filename)
        if os.path.exists(filepath):
            duration = get_audio_duration(filepath)
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            diagnostics[f"{filename}_duration"] = duration
            diagnostics[f"{filename}_size_mb"] = round(size_mb, 2)
    
    # Check scene.py line count
    scene_path = os.path.join(folder_path, "scene.py")
    if os.path.exists(scene_path):
        with open(scene_path, "r") as f:
            scene_lines = len(f.readlines())
            diagnostics["scene_file_lines"] = scene_lines
    
    return JSONResponse(diagnostics)

if __name__ == "__main__":
    import uvicorn
    print("="*70)
    print("🎓 Adaptive Educational Video Generator v4.0")
    print("="*70)
    print(f"Output directory: {OUTPUT_DIR}")
    print("\nKey Features:")
    print("  ✓ Intelligent duration control (20s to 6min)")
    print("  ✓ Step-by-step visual calculations")
    print("  ✓ Procedural vs conceptual topic detection")
    print("  ✓ Natural, engaging narration")
    print("  ✓ Professional Manim animations")
    print("  ✓ Parallel audio processing")
    print("="*70)
    print("\nStarting server on http://0.0.0.0:8000")
    print("="*70)
    uvicorn.run(app, host="0.0.0.0", port=8000)
