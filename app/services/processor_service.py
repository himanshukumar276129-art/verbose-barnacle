import os
import shutil
import tempfile
import uuid
from typing import Any

import requests

from app.background.background_remover import BackgroundRemover
from app.enhancer.image_enhancer import ImageEnhancer
from app.ffmpeg.video_processor import VideoProcessor
from app.storage.storage_manager import storage_manager
from app.watermark.watermark_remover import WatermarkRemover


background_remover = BackgroundRemover()
image_enhancer = ImageEnhancer()
watermark_remover = WatermarkRemover()


class ProcessorService:
    def __init__(self, timeout_seconds: int, max_download_mb: int):
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_mb * 1024 * 1024

    def download_to_temp(self, source_url: str, suffix: str) -> str:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        downloaded_bytes = 0

        try:
            with requests.get(source_url, stream=True, timeout=self.timeout_seconds) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > self.max_download_bytes:
                        raise ValueError(f"Remote file exceeds {self.max_download_bytes} bytes limit.")
                    temp_file.write(chunk)
        finally:
            temp_file.close()

        return temp_file.name

    def process(self, source_url: str, asset_type: str, job_type: str, mime_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        suffix = self._suffix_from_mime(mime_type)
        source_path = self.download_to_temp(source_url, suffix)

        try:
            if job_type == "IMAGE_BACKGROUND_REMOVAL":
                return self._process_image_background_removal(source_path, options)
            if job_type == "VIDEO_BACKGROUND_REMOVAL":
                return self._process_video_background_removal(source_path, options)
            if job_type == "IMAGE_WATERMARK_REMOVAL":
                return self._process_image_watermark_removal(source_path, options)
            if job_type == "VIDEO_WATERMARK_REMOVAL":
                return self._process_video_watermark_removal(source_path, options)
            if job_type == "IMAGE_ENHANCEMENT":
                return self._process_image_enhancement(source_path, options)
            if job_type == "VIDEO_ENHANCEMENT":
                return self._process_video_enhancement(source_path, options)

            raise ValueError(f"Unsupported job type: {job_type} for asset type: {asset_type}")
        finally:
            if os.path.exists(source_path):
                os.remove(source_path)

    def _process_image_background_removal(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        output_filename = f"bg_removed_{uuid.uuid4().hex}.png"
        output_path = storage_manager.get_local_path(output_filename)
        background_color = self._background_tuple(options.get("backgroundColor"))

        background_remover.remove_background_image(source_path, output_path, background_color)
        output_url = storage_manager.upload_file(output_path, output_filename)
        return {"provider": "python-media-processor", "outputUrl": output_url}

    def _process_video_background_removal(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        temp_dir = tempfile.mkdtemp(prefix="bg_video_")
        try:
            info = VideoProcessor.get_video_info(source_path)
            frame_dir = os.path.join(temp_dir, "frames")
            VideoProcessor.extract_frames(source_path, frame_dir)
            background_color = self._background_tuple(options.get("backgroundColor")) or (255, 255, 255)

            for frame_name in sorted(name for name in os.listdir(frame_dir) if name.endswith(".png")):
                frame_path = os.path.join(frame_dir, frame_name)
                import cv2

                frame = cv2.imread(frame_path)
                processed = background_remover.remove_background_frame(frame, background_color)
                cv2.imwrite(frame_path, processed)

            assembled_path = os.path.join(temp_dir, "assembled.mp4")
            VideoProcessor.assemble_video(frame_dir, source_path, assembled_path, info["fps"], info["has_audio"])
            compressed_path = os.path.join(temp_dir, "compressed.mp4")
            VideoProcessor.compress_video(assembled_path, compressed_path)

            output_filename = f"bg_removed_{uuid.uuid4().hex}.mp4"
            output_url = storage_manager.upload_file(compressed_path, output_filename)
            return {"provider": "python-media-processor", "outputUrl": output_url}
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    def _process_image_watermark_removal(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        mask_url = options.get("maskUrl")
        if not mask_url:
            raise ValueError("maskUrl is required for image watermark removal.")

        mask_path = self.download_to_temp(mask_url, ".png")
        try:
            output_filename = f"watermark_removed_{uuid.uuid4().hex}.png"
            output_path = storage_manager.get_local_path(output_filename)
            watermark_remover.remove_watermark(source_path, mask_path, output_path, options.get("algorithm", "telea"))
            output_url = storage_manager.upload_file(output_path, output_filename)
            return {"provider": "python-media-processor", "outputUrl": output_url}
        finally:
            if os.path.exists(mask_path):
                os.remove(mask_path)

    def _process_video_watermark_removal(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        mask_url = options.get("maskUrl")
        if not mask_url:
            raise ValueError("maskUrl is required for video watermark removal.")

        mask_path = self.download_to_temp(mask_url, ".png")
        temp_dir = tempfile.mkdtemp(prefix="watermark_video_")

        try:
            info = VideoProcessor.get_video_info(source_path)
            frame_dir = os.path.join(temp_dir, "frames")
            VideoProcessor.extract_frames(source_path, frame_dir)

            for frame_name in sorted(name for name in os.listdir(frame_dir) if name.endswith(".png")):
                frame_path = os.path.join(frame_dir, frame_name)
                watermark_remover.remove_watermark(frame_path, mask_path, frame_path, options.get("algorithm", "telea"))

            assembled_path = os.path.join(temp_dir, "assembled.mp4")
            VideoProcessor.assemble_video(frame_dir, source_path, assembled_path, info["fps"], info["has_audio"])
            compressed_path = os.path.join(temp_dir, "compressed.mp4")
            VideoProcessor.compress_video(assembled_path, compressed_path)

            output_filename = f"watermark_removed_{uuid.uuid4().hex}.mp4"
            output_url = storage_manager.upload_file(compressed_path, output_filename)
            return {"provider": "python-media-processor", "outputUrl": output_url}
        finally:
            if os.path.exists(mask_path):
                os.remove(mask_path)
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    def _process_image_enhancement(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        output_filename = f"enhanced_{uuid.uuid4().hex}.png"
        output_path = storage_manager.get_local_path(output_filename)
        image_enhancer.enhance(
            source_path,
            output_path,
            scale=int(options.get("scale", 4)),
            face_enhance=bool(options.get("faceEnhance", False)),
            denoise=bool(options.get("denoise", True)),
            sharpen=bool(options.get("sharpen", True)),
        )
        output_url = storage_manager.upload_file(output_path, output_filename)
        return {"provider": "python-media-processor", "outputUrl": output_url}

    def _process_video_enhancement(self, source_path: str, options: dict[str, Any]) -> dict[str, Any]:
        temp_dir = tempfile.mkdtemp(prefix="enhance_video_")
        try:
            info = VideoProcessor.get_video_info(source_path)
            frame_dir = os.path.join(temp_dir, "frames")
            VideoProcessor.extract_frames(source_path, frame_dir)

            for frame_name in sorted(name for name in os.listdir(frame_dir) if name.endswith(".png")):
                frame_path = os.path.join(frame_dir, frame_name)
                image_enhancer.enhance(
                    frame_path,
                    frame_path,
                    scale=int(options.get("scale", 2)),
                    face_enhance=bool(options.get("faceEnhance", False)),
                    denoise=bool(options.get("denoise", True)),
                    sharpen=bool(options.get("sharpen", True)),
                )

            assembled_path = os.path.join(temp_dir, "assembled.mp4")
            VideoProcessor.assemble_video(frame_dir, source_path, assembled_path, info["fps"], info["has_audio"])
            compressed_path = os.path.join(temp_dir, "compressed.mp4")
            VideoProcessor.compress_video(assembled_path, compressed_path)

            output_filename = f"enhanced_{uuid.uuid4().hex}.mp4"
            output_url = storage_manager.upload_file(compressed_path, output_filename)
            return {"provider": "python-media-processor", "outputUrl": output_url}
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    @staticmethod
    def _suffix_from_mime(mime_type: str) -> str:
        if "png" in mime_type:
            return ".png"
        if "jpeg" in mime_type or "jpg" in mime_type:
            return ".jpg"
        if "webp" in mime_type:
            return ".webp"
        if "quicktime" in mime_type:
            return ".mov"
        if "webm" in mime_type:
            return ".webm"
        if "mp4" in mime_type:
            return ".mp4"
        return ".bin"

    @staticmethod
    def _background_tuple(value: Any) -> tuple[int, int, int] | None:
        if value is None:
            return None
        if isinstance(value, list) and len(value) == 3:
            return tuple(int(channel) for channel in value)
        if isinstance(value, str) and value.startswith("#") and len(value) == 7:
            return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
        return (255, 255, 255)
