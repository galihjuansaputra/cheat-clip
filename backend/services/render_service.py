import asyncio
import logging
import os
import re
import subprocess
import zipfile
from typing import Any, Dict, List, Optional

from backend.config import (
    EXPORTS_DIR,
    TEMP_DIR,
    download_clip_segment,
    generate_ass_file,
    has_emoji,
    is_valid_mp4,
    logger,
    render_clip_to_mp4,
    render_title_overlay_png,
    transcribe_clip_words,
)
from backend.schemas.render import RenderBatchRequest, RenderSettingsModel

RENDER_BATCHES: Dict[str, Dict[str, Any]] = {}
BATCH_REQUESTS: Dict[str, RenderBatchRequest] = {}


async def render_single_batch_clip(
    batch_id: str,
    idx: int,
    clip: Dict[str, Any],
    settings: RenderSettingsModel,
    target_url: str,
    transcript: Optional[List[Dict[str, Any]]] = None,
    total_clips: int = 1
):
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    clip_status = batch["clips"][idx]
    clip_status["status"] = "downloading"
    clip_status["progress_percent"] = 15
    clip_status["error_message"] = None
    clip_status["error"] = None

    raw_path = None
    ass_path = None
    title_overlay_path = None

    try:
        # 1. Download
        start_t = float(clip.get("start_time", 0.0))
        end_t = float(clip.get("end_time", start_t + 30.0))
        seg_filename = f"{batch_id}_clip_{idx}_raw.mp4"

        raw_path = await asyncio.to_thread(
            download_clip_segment,
            target_url,
            start_t,
            end_t,
            seg_filename
        )

        if not raw_path or not os.path.exists(raw_path) or not is_valid_mp4(raw_path):
            raise RuntimeError(
                "Source video segment is incomplete or corrupted ('moov atom not found'). "
                "Network lag interrupted the download. Please retry rendering this clip."
            )

        # 2. Transcribe & Generate Subtitles / Title (.ass)
        clip_status["status"] = "transcribing"
        clip_status["progress_percent"] = 40

        display_title = None
        base_title = (
            clip.get("custom_title")
            or clip.get("title_suggestion")
            or clip.get("title")
            or f"Clip {idx+1}"
        ).strip()

        if settings.title_position != "none":
            pfx = settings.title_prefix or ""
            sfx = settings.title_suffix or ""
            if pfx or sfx:
                display_title = f"{pfx}{base_title}{sfx}".strip()
            elif total_clips == 1 and settings.title_text and settings.title_text.strip() and not (clip.get("custom_title") or clip.get("title_suggestion")):
                display_title = settings.title_text.strip()
            else:
                display_title = base_title

        clip_status["title"] = display_title or base_title
        clip_status["base_title"] = base_title

        skip_ass_title = False
        duration_sec = max(1.0, end_t - start_t)

        # Check if title has emoji -> render transparent color emoji PNG overlay
        if display_title and settings.title_position != "none" and has_emoji(display_title):
            title_png_filename = f"{batch_id}_clip_{idx}_title.png"
            title_png_path = str(TEMP_DIR / title_png_filename)
            try:
                canvas_w = 1920 if (settings.aspect_ratio == "16:9_landscape") else 1080
                canvas_h = 1080 if (settings.aspect_ratio == "16:9_landscape") else 1920
                rendered_overlay = await asyncio.to_thread(
                    render_title_overlay_png,
                    title_text=display_title,
                    output_png_path=title_png_path,
                    font_name=settings.title_font or settings.caption_font or "Outfit",
                    target_aspect_ratio=settings.aspect_ratio or "9:16",
                    font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    text_case=settings.title_text_case or settings.text_case or "uppercase",
                    title_position=settings.title_position or "auto",
                    title_y_percent=settings.title_y_percent,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    streamer_preset=settings.streamer_preset or "none",
                    title_font_name=settings.title_font or settings.caption_font or "Outfit",
                    title_font_size_px=settings.title_font_size_px,
                    title_text_case=settings.title_text_case or settings.text_case or "uppercase"
                )
                if rendered_overlay and os.path.exists(rendered_overlay):
                    title_overlay_path = rendered_overlay
                    skip_ass_title = True
                    logger.info(f"Rendered full-color emoji title overlay: {title_overlay_path}")
            except Exception as ex:
                logger.warning(f"Could not render color emoji title overlay: {ex}")
                skip_ass_title = False

        if settings.caption_style != "none" or (display_title and settings.title_position != "none" and not skip_ass_title):
            words = []
            if settings.caption_style != "none":
                words = await asyncio.to_thread(
                    transcribe_clip_words,
                    raw_path,
                    transcript,
                    start_t,
                    end_t
                )
            ass_filename = f"{batch_id}_clip_{idx}.ass"
            ass_path = str(TEMP_DIR / ass_filename)
            await asyncio.to_thread(
                generate_ass_file,
                words=words,
                style_preset=settings.caption_style,
                font_name=settings.caption_font or "Outfit",
                output_ass_path=ass_path,
                target_aspect_ratio=settings.aspect_ratio,
                font_size_preset=settings.font_size,
                text_case=settings.text_case,
                title_text=display_title if not skip_ass_title else None,
                title_position=settings.title_position,
                title_duration=settings.title_duration if settings.title_duration else "entire",
                duration_seconds=duration_sec,
                title_y_percent=settings.title_y_percent,
                subtitle_y_percent=settings.subtitle_y_percent,
                subtitle_position_mode=settings.subtitle_position_mode if settings.subtitle_position_mode else "bottom",
                subtitle_center_y_percent=settings.subtitle_center_y_percent if settings.subtitle_center_y_percent is not None else 50.0,
                skip_title=skip_ass_title,
                title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                streamer_preset=settings.streamer_preset or "none",
                title_font_name=settings.title_font or settings.caption_font or "Outfit",
                font_size_px=settings.font_size_px,
                title_font_size_px=settings.title_font_size_px,
                title_text_case=settings.title_text_case or settings.text_case or "uppercase"
            )

        # 3. Render Final Vertical MP4
        clip_status["status"] = "rendering"
        clip_status["progress_percent"] = 70

        out_filename = f"clip_{idx+1}_{batch_id}.mp4"
        out_path = str(EXPORTS_DIR / out_filename)

        await asyncio.to_thread(
            render_clip_to_mp4,
            video_path=raw_path,
            output_mp4_path=out_path,
            aspect_ratio=settings.aspect_ratio,
            background_style=settings.background_style,
            enable_face_tracking=settings.enable_face_tracking,
            streamer_preset=settings.streamer_preset,
            facecam_position=getattr(settings, "facecam_position", "auto") or "auto",
            title_text=display_title if not skip_ass_title else None,
            title_position=settings.title_position,
            ass_subtitles_path=ass_path,
            clip_duration=duration_sec,
            title_overlay_path=title_overlay_path,
            title_duration=settings.title_duration if settings.title_duration else "entire",
            watermark_enabled=bool(settings.watermark_enabled),
            watermark_type=settings.watermark_type or "image",
            watermark_image_path=settings.watermark_file_path,
            watermark_text=settings.watermark_text,
            watermark_size=float(settings.watermark_size if settings.watermark_size is not None else 20.0),
            watermark_opacity=float((settings.watermark_opacity if settings.watermark_opacity is not None else 80.0) / 100.0),
            watermark_x_percent=float(settings.watermark_x if settings.watermark_x is not None else 90.0),
            watermark_y_percent=float(settings.watermark_y if settings.watermark_y is not None else 8.0),
            bgm_enabled=bool(settings.bgm_enabled),
            bgm_path=settings.bgm_file_path,
            bgm_volume=float((settings.bgm_volume if settings.bgm_volume is not None else 25.0) / 100.0),
            bgm_start_offset=float(settings.bgm_start_offset or 0.0),
            hook_sfx_enabled=bool(settings.hook_sfx_enabled),
            hook_sfx_path=settings.hook_sfx_file_path,
            hook_sfx_volume=float((settings.hook_sfx_volume if settings.hook_sfx_volume is not None else 100.0) / 100.0),
            original_audio_volume=float((settings.original_audio_volume if settings.original_audio_volume is not None else 100.0) / 100.0),
            hardware_accel=settings.hardware_accel or "auto",
            title_y_percent=settings.title_y_percent
        )

        if not os.path.exists(out_path) or not is_valid_mp4(out_path):
            raise RuntimeError("Rendered MP4 file is incomplete or missing. Please retry rendering.")

        clip_status["status"] = "completed"
        clip_status["progress_percent"] = 100
        clip_status["download_url"] = f"/api/download-rendered/{out_filename}"
        clip_status["output_path"] = out_path

    except Exception as e:
        logger.error(f"Error rendering clip {idx} in batch {batch_id}: {e}")
        clip_status["status"] = "error"
        err_msg = str(e)
        if "moov atom not found" in err_msg.lower():
            err_msg = "Download interrupted by internet lag ('moov atom not found'). Click Retry to re-download."
        elif "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
            if clip_status.get("status") == "rendering":
                err_msg = "Video rendering timed out. Try switching to 'Universal CPU (libx264)' in Studio Settings or retry."
            else:
                err_msg = "Video download timed out due to slow/laggy internet connection. Click Retry to try again."
        clip_status["error_message"] = err_msg
        clip_status["error"] = err_msg

        # Clean up any partial raw video
        if raw_path and os.path.exists(raw_path):
            try:
                os.unlink(raw_path)
            except Exception:
                pass


def update_batch_summary_and_zip(batch_id: str, settings: RenderSettingsModel):
    """
    Updates the batch ZIP archive and computes overall status and friendly messages.
    """
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    # Generate/update ZIP bundle for the batch with title-based filenames and duplicate handling
    try:
        completed_clips = [c for c in batch["clips"] if c.get("status") == "completed" and c.get("download_url")]
        if completed_clips:
            zip_filename = f"cheat_clip_pro_{batch_id}.zip"
            zip_path = EXPORTS_DIR / zip_filename
            title_counts: Dict[str, int] = {}
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for c in completed_clips:
                    fname = c["download_url"].split("?")[0].split("/")[-1]
                    fpath = EXPORTS_DIR / fname
                    if fpath.exists():
                        raw_title = (c.get("base_title") or c.get("title") or "").strip()
                        clean_title = re.sub(r'[\\/*?:"<>|]', "", raw_title) or f"clip_{c.get('clip_index', 1)}"
                        fn_pfx = re.sub(r'[\\/*?:"<>|]', "", settings.file_name_prefix or "")
                        fn_sfx = re.sub(r'[\\/*?:"<>|]', "", settings.file_name_suffix or "")
                        formatted_name = f"{fn_pfx}{clean_title}{fn_sfx}".strip() or clean_title
                        count = title_counts.get(formatted_name, 0)
                        title_counts[formatted_name] = count + 1
                        arc_name = f"{formatted_name}.mp4" if count == 0 else f"{formatted_name} ({count}).mp4"
                        zipf.write(fpath, arcname=arc_name)
            batch["zip_url"] = f"/api/download-batch-zip/{batch_id}"
        else:
            batch["zip_url"] = None
    except Exception as e:
        logger.warning(f"Failed to create batch zip: {e}")

    # Determine overall status and error messaging
    failed_clips = [c for c in batch["clips"] if c.get("status") == "error"]
    running_clips = [c for c in batch["clips"] if c.get("status") in ["downloading", "transcribing", "rendering", "pending"]]

    if running_clips:
        batch["overall_status"] = "running"
    elif len(failed_clips) == len(batch["clips"]):
        batch["overall_status"] = "error"
        batch["error_message"] = f"All {len(batch['clips'])} clip(s) failed. You can click 'Retry' to try again."
    elif len(failed_clips) > 0:
        batch["overall_status"] = "completed"
        batch["warning_message"] = f"{len(failed_clips)} of {len(batch['clips'])} clips encountered errors. You can retry failed clips anytime."
    else:
        batch["overall_status"] = "completed"
        batch["warning_message"] = None


async def render_merged_batch_clips(
    batch_id: str,
    clips: List[Dict[str, Any]],
    settings: RenderSettingsModel,
    target_url: str,
    transcript: Optional[List[Dict[str, Any]]] = None
):
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    clip_status = batch["clips"][0]
    clip_status["status"] = "downloading"
    clip_status["progress_percent"] = 10
    clip_status["error_message"] = None
    clip_status["error"] = None

    part_paths: List[str] = []
    raw_merged_path: Optional[str] = None
    ass_path: Optional[str] = None
    title_overlay_path: Optional[str] = None

    try:
        total_segments = len(clips)
        # 1. Download / slice each segment
        for i, seg in enumerate(clips):
            clip_status["progress_percent"] = 10 + int((i / max(1, total_segments)) * 25)
            s_t = float(seg.get("start_time", 0.0))
            e_t = float(seg.get("end_time", s_t + 30.0))
            part_filename = f"{batch_id}_part_{i}_raw.mp4"
            p_path = await asyncio.to_thread(
                download_clip_segment,
                target_url,
                s_t,
                e_t,
                part_filename
            )
            if not p_path or not os.path.exists(p_path) or not is_valid_mp4(p_path):
                raise RuntimeError(
                    f"Segment #{i+1} ({s_t:.1f}s - {e_t:.1f}s) is incomplete or corrupted. Please retry rendering."
                )
            part_paths.append(p_path)

        # 2. Concatenate raw video segments
        clip_status["status"] = "transcribing"
        clip_status["progress_percent"] = 40

        raw_merged_filename = f"{batch_id}_merged_raw.mp4"
        raw_merged_path = str(TEMP_DIR / raw_merged_filename)

        if len(part_paths) == 1:
            raw_merged_path = part_paths[0]
        else:
            inputs = []
            filter_parts = []
            for idx_p, p in enumerate(part_paths):
                inputs.extend(["-i", p])
                filter_parts.append(f"[{idx_p}:v][{idx_p}:a]")
            filter_str = f"{''.join(filter_parts)}concat=n={len(part_paths)}:v=1:a=1[v][a]"
            concat_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *inputs,
                "-filter_complex", filter_str,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                raw_merged_path
            ]
            concat_timeout = max(180, total_segments * 60)
            logger.info(f"Concatenating {len(part_paths)} segments into {raw_merged_path}")
            await asyncio.to_thread(subprocess.run, concat_cmd, check=True, timeout=concat_timeout)

        # 3. Transcribe & generate time-shifted subtitles
        total_duration_sec = 0.0
        merged_words = []
        for i, seg in enumerate(clips):
            s_t = float(seg.get("start_time", 0.0))
            e_t = float(seg.get("end_time", s_t + 30.0))
            seg_dur = max(0.1, e_t - s_t)
            if settings.caption_style != "none":
                seg_words = await asyncio.to_thread(
                    transcribe_clip_words,
                    part_paths[i],
                    transcript,
                    s_t,
                    e_t
                )
                for w in seg_words:
                    w_copy = dict(w)
                    w_start = float(w.get("start", 0.0))
                    w_end = float(w.get("end", w_start + 0.15))
                    w_copy["start"] = round(total_duration_sec + max(0.0, w_start), 3)
                    w_copy["end"] = round(max(w_copy["start"] + 0.1, total_duration_sec + max(0.0, w_end)), 3)
                    merged_words.append(w_copy)
            total_duration_sec += seg_dur

        total_duration_sec = max(1.0, total_duration_sec)

        display_title = clip_status.get("title") or clip_status.get("base_title") or "Highlight Video"
        skip_ass_title = False

        if display_title and settings.title_position != "none" and has_emoji(display_title):
            title_png_filename = f"{batch_id}_merged_title.png"
            title_png_path = str(TEMP_DIR / title_png_filename)
            try:
                canvas_w = 1920 if (settings.aspect_ratio == "16:9_landscape") else 1080
                canvas_h = 1080 if (settings.aspect_ratio == "16:9_landscape") else 1920
                rendered_overlay = await asyncio.to_thread(
                    render_title_overlay_png,
                    title_text=display_title,
                    output_png_path=title_png_path,
                    font_name=settings.title_font or settings.caption_font or "Outfit",
                    target_aspect_ratio=settings.aspect_ratio or "9:16",
                    font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    text_case=settings.title_text_case or settings.text_case or "uppercase",
                    title_position=settings.title_position or "auto",
                    title_y_percent=settings.title_y_percent,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    streamer_preset=settings.streamer_preset or "none",
                    title_font_name=settings.title_font or settings.caption_font or "Outfit",
                    title_font_size_px=settings.title_font_size_px,
                    title_text_case=settings.title_text_case or settings.text_case or "uppercase"
                )
                if rendered_overlay and os.path.exists(rendered_overlay):
                    title_overlay_path = rendered_overlay
                    skip_ass_title = True
            except Exception as ex:
                logger.warning(f"Could not render color emoji title overlay for merged clip: {ex}")
                skip_ass_title = False

        if settings.caption_style != "none" or (display_title and settings.title_position != "none" and not skip_ass_title):
            ass_filename = f"{batch_id}_merged.ass"
            ass_path = str(TEMP_DIR / ass_filename)
            await asyncio.to_thread(
                generate_ass_file,
                words=merged_words,
                style_preset=settings.caption_style,
                font_name=settings.caption_font or "Outfit",
                output_ass_path=ass_path,
                target_aspect_ratio=settings.aspect_ratio,
                font_size_preset=settings.font_size,
                text_case=settings.text_case,
                title_text=display_title if not skip_ass_title else None,
                title_position=settings.title_position,
                title_duration=settings.title_duration if settings.title_duration else "entire",
                duration_seconds=total_duration_sec,
                title_y_percent=settings.title_y_percent,
                subtitle_y_percent=settings.subtitle_y_percent,
                subtitle_position_mode=settings.subtitle_position_mode if settings.subtitle_position_mode else "bottom",
                subtitle_center_y_percent=settings.subtitle_center_y_percent if settings.subtitle_center_y_percent is not None else 50.0,
                skip_title=skip_ass_title,
                title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                streamer_preset=settings.streamer_preset or "none",
                title_font_name=settings.title_font or settings.caption_font or "Outfit",
                font_size_px=settings.font_size_px,
                title_font_size_px=settings.title_font_size_px,
                title_text_case=settings.title_text_case or settings.text_case or "uppercase"
            )

        # 4. Render Final Single MP4
        clip_status["status"] = "rendering"
        clip_status["progress_percent"] = 65

        clean_safe_title = re.sub(r'[\\/*?:"<>|]', "", clip_status.get("base_title") or "highlight_compilation")
        clean_safe_title = clean_safe_title.replace(" ", "_")[:40] or "compilation"
        out_filename = f"compilation_{clean_safe_title}_{batch_id}.mp4"
        out_path = str(EXPORTS_DIR / out_filename)

        await asyncio.to_thread(
            render_clip_to_mp4,
            video_path=raw_merged_path,
            output_mp4_path=out_path,
            aspect_ratio=settings.aspect_ratio,
            background_style=settings.background_style,
            enable_face_tracking=settings.enable_face_tracking,
            streamer_preset=settings.streamer_preset,
            facecam_position=getattr(settings, "facecam_position", "auto") or "auto",
            title_text=display_title if not skip_ass_title else None,
            title_position=settings.title_position,
            ass_subtitles_path=ass_path,
            clip_duration=total_duration_sec,
            title_overlay_path=title_overlay_path,
            title_duration=settings.title_duration if settings.title_duration else "entire",
            watermark_enabled=bool(settings.watermark_enabled),
            watermark_type=settings.watermark_type or "image",
            watermark_image_path=settings.watermark_file_path,
            watermark_text=settings.watermark_text,
            watermark_size=float(settings.watermark_size if settings.watermark_size is not None else 20.0),
            watermark_opacity=float((settings.watermark_opacity if settings.watermark_opacity is not None else 80.0) / 100.0),
            watermark_x_percent=float(settings.watermark_x if settings.watermark_x is not None else 90.0),
            watermark_y_percent=float(settings.watermark_y if settings.watermark_y is not None else 8.0),
            bgm_enabled=bool(settings.bgm_enabled),
            bgm_path=settings.bgm_file_path,
            bgm_volume=float((settings.bgm_volume if settings.bgm_volume is not None else 25.0) / 100.0),
            bgm_start_offset=float(settings.bgm_start_offset or 0.0),
            hook_sfx_enabled=bool(settings.hook_sfx_enabled),
            hook_sfx_path=settings.hook_sfx_file_path,
            hook_sfx_volume=float((settings.hook_sfx_volume if settings.hook_sfx_volume is not None else 100.0) / 100.0),
            original_audio_volume=float((settings.original_audio_volume if settings.original_audio_volume is not None else 100.0) / 100.0),
            hardware_accel=settings.hardware_accel or "auto",
            title_y_percent=settings.title_y_percent
        )

        if not os.path.exists(out_path) or not is_valid_mp4(out_path):
            raise RuntimeError("Merged MP4 file is incomplete or missing. Please retry rendering.")

        clip_status["status"] = "completed"
        clip_status["progress_percent"] = 100
        clip_status["download_url"] = f"/api/download-rendered/{out_filename}"
        clip_status["output_path"] = out_path

    except Exception as e:
        logger.error(f"Error rendering merged batch {batch_id}: {e}")
        clip_status["status"] = "error"
        err_msg = str(e)
        clip_status["error_message"] = err_msg
        clip_status["error"] = err_msg

    finally:
        for p in part_paths:
            if p and os.path.exists(p) and p != raw_merged_path:
                try:
                    os.unlink(p)
                except Exception:
                    pass
        if raw_merged_path and os.path.exists(raw_merged_path):
            try:
                os.unlink(raw_merged_path)
            except Exception:
                pass


async def process_batch_rendering(batch_id: str, request: RenderBatchRequest):
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    clips = request.clips
    settings = request.settings

    # Normalize video URL for history or direct URL
    target_url = (request.video_url or "").strip()
    if not target_url:
        if request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            target_url = f"/api/video/{request.video_id}"
        elif request.video_id:
            target_url = f"https://www.youtube.com/watch?v={request.video_id}"
    elif not target_url.startswith("http") and not target_url.startswith("/api/video/"):
        if request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            target_url = f"/api/video/{request.video_id}"
        elif request.video_id:
            target_url = f"https://www.youtube.com/watch?v={request.video_id}"
        else:
            target_url = f"https://www.youtube.com/watch?v={target_url}"

    is_merged = bool(settings and getattr(settings, "render_mode", "separate") == "merged")
    if is_merged:
        batch["current_clip_index"] = 0
        await render_merged_batch_clips(
            batch_id=batch_id,
            clips=clips,
            settings=settings,
            target_url=target_url,
            transcript=request.transcript
        )
        batch["current_clip_index"] = 1
    else:
        for idx, clip in enumerate(clips):
            batch["current_clip_index"] = idx
            await render_single_batch_clip(
                batch_id=batch_id,
                idx=idx,
                clip=clip,
                settings=settings,
                target_url=target_url,
                transcript=request.transcript,
                total_clips=len(clips)
            )
            batch["current_clip_index"] = idx + 1

    update_batch_summary_and_zip(batch_id, settings)


async def process_batch_retry(batch_id: str, clip_indices: List[int]):
    batch = RENDER_BATCHES.get(batch_id)
    request = BATCH_REQUESTS.get(batch_id)
    if not batch or not request:
        logger.error(f"Cannot retry batch {batch_id}: batch or request data not found")
        return

    batch["overall_status"] = "running"
    clips = request.clips
    settings = request.settings
    target_url = (request.video_url or "").strip()
    if not target_url:
        if request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            target_url = f"/api/video/{request.video_id}"
        elif request.video_id:
            target_url = f"https://www.youtube.com/watch?v={request.video_id}"
    elif not target_url.startswith("http") and not target_url.startswith("/api/video/"):
        if request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            target_url = f"/api/video/{request.video_id}"
        elif request.video_id:
            target_url = f"https://www.youtube.com/watch?v={request.video_id}"
        else:
            target_url = f"https://www.youtube.com/watch?v={target_url}"

    is_merged = bool(batch.get("is_merged")) or bool(settings and getattr(settings, "render_mode", "separate") == "merged")
    if is_merged:
        batch["current_clip_index"] = 0
        await render_merged_batch_clips(
            batch_id=batch_id,
            clips=clips,
            settings=settings,
            target_url=target_url,
            transcript=request.transcript
        )
        batch["current_clip_index"] = 1
    else:
        for idx in clip_indices:
            if 0 <= idx < len(clips):
                batch["current_clip_index"] = idx
                await render_single_batch_clip(
                    batch_id=batch_id,
                    idx=idx,
                    clip=clips[idx],
                    settings=settings,
                    target_url=target_url,
                    transcript=request.transcript,
                    total_clips=len(clips)
                )

    update_batch_summary_and_zip(batch_id, settings)
