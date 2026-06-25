"""
AudioRecaptchaSolver - A class to solve reCAPTCHA using audio challenge and Whisper transcription.

Requirements:
    pip install -U faster-whisper seleniumbase requests

FFmpeg Installation:
    Mac:     brew install ffmpeg
    Windows: winget install Gyan.FFmpeg
    Ubuntu:  sudo apt-get update && sudo apt-get install -y ffmpeg
"""

from __future__ import annotations

import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Union, List

from faster_whisper import WhisperModel
from seleniumbase import SB


# ─────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ─────────────────────────────────────────────────────────────────────────────
AudioPath = Union[str, Path]
Device = Literal["cpu", "cuda"]


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TranscriptionResult:
    """Result of audio transcription."""
    text: str
    language: str
    language_probability: float


@dataclass
class SolveResult:
    """Result of CAPTCHA solving attempt."""
    success: bool
    message: str
    transcription: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# AudioRecaptchaSolver Class
# ─────────────────────────────────────────────────────────────────────────────
class AudioRecaptchaSolver:
    """
    Solves reCAPTCHA challenges using audio transcription with Whisper.
    
    Usage:
        solver = AudioRecaptchaSolver()
        
        # With SeleniumBase
        with SB(uc=True) as sb:
            sb.open("https://example.com/page-with-captcha")
            if solver.is_captcha_present(sb):
                result = solver.solve(sb)
                if result.success:
                    print("CAPTCHA solved!")
    """
    
    # CAPTCHA detection indicators
    CAPTCHA_INDICATORS = [
        "unusual traffic",
        "automated requests",
        "captcha",
        "recaptcha",
        "sorry...",
        "our systems have detected",
    ]
    
    # reCAPTCHA selectors
    RECAPTCHA_IFRAME = "iframe[title='reCAPTCHA']"
    CHALLENGE_IFRAME = "iframe[title='recaptcha challenge expires in two minutes']"
    ANCHOR_SELECTOR = "span#recaptcha-anchor"
    AUDIO_BUTTON = "button#recaptcha-audio-button"
    AUDIO_SOURCE = "audio#audio-source"
    AUDIO_RESPONSE = "input#audio-response"
    VERIFY_BUTTON = "button#recaptcha-verify-button"
    
    def __init__(
        self,
        model_name: str = "small",
        device: Device = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = "en",
        downloads_dir: Union[str, Path] = "downloads",
        cleanup_audio: bool = True,
    ):
        """
        Initialize the AudioRecaptchaSolver.
        
        Args:
            model_name: Whisper model - "tiny", "base", "small", "medium", "large-v3"
            device: "cpu" or "cuda"
            compute_type: CPU: "int8", "int16", "float32"; CUDA: "float16", "int8_float16"
            language: Language code (e.g., "en") or None for auto-detect
            downloads_dir: Directory to save downloaded audio files
            cleanup_audio: Whether to delete audio files after transcription
        """
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.downloads_dir = Path(downloads_dir)
        self.cleanup_audio = cleanup_audio
        self._model: Optional[WhisperModel] = None
    
    @property
    def model(self) -> WhisperModel:
        """Lazy-load the Whisper model."""
        if self._model is None:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type
            )
        return self._model
    
    def is_captcha_present(self, sb: SB) -> bool:
        """
        Check if a CAPTCHA challenge is actively blocking the page.
        
        Args:
            sb: SeleniumBase browser instance
            
        Returns:
            True if CAPTCHA challenge is actively present
        """
        try:
            # First check for visible reCAPTCHA iframe (more reliable)
            try:
                if sb.is_element_visible(self.RECAPTCHA_IFRAME):
                    return True
            except Exception:
                pass
            
            # Check for challenge iframe
            try:
                if sb.is_element_visible(self.CHALLENGE_IFRAME):
                    return True
            except Exception:
                pass
            
            # Check page source for blocking indicators (unusual traffic, etc.)
            page_source = sb.get_page_source().lower()
            blocking_indicators = ["unusual traffic", "automated requests", "our systems have detected", "sorry..."]
            return any(indicator in page_source for indicator in blocking_indicators)
        except Exception:
            return False
    
    def is_captcha_solved(self, sb: SB) -> bool:
        """
        Check if the captcha has been solved successfully.
        
        Args:
            sb: SeleniumBase browser instance
            
        Returns:
            True if captcha appears to be solved
        """
        try:
            # If challenge iframe is not visible and no blocking text, captcha is solved
            try:
                if sb.is_element_visible(self.CHALLENGE_IFRAME):
                    return False  # Challenge still visible
            except Exception:
                pass
            
            # Check for blocking indicators
            page_source = sb.get_page_source().lower()
            blocking_indicators = ["unusual traffic", "automated requests", "our systems have detected"]
            if any(indicator in page_source for indicator in blocking_indicators):
                return False
            
            return True
        except Exception:
            return True  # Assume solved if we can't check
    
    def _download_audio(self, sb: SB, url: str, filename: str) -> Path:
        """
        Download audio file from URL using browser session cookies.
        
        Args:
            sb: SeleniumBase browser instance
            url: Audio file URL
            filename: Name for the downloaded file
            
        Returns:
            Path to the downloaded file
        """
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        dest = self.downloads_dir / filename
        
        # Use browser cookies for authenticated download
        session = requests.Session()
        for cookie in sb.driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])
        
        response = session.get(url, stream=True)
        response.raise_for_status()
        
        with open(dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return dest
    
    def _transcribe(self, audio_path: Path) -> TranscriptionResult:
        """
        Transcribe an audio file using Whisper.
        
        Args:
            audio_path: Path to the audio file
            
        Returns:
            TranscriptionResult with text and language info
        """
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.language,
            vad_filter=True,
            beam_size=5,
            temperature=0.0,
        )
        
        lines = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
        
        return TranscriptionResult(
            text=" ".join(lines).strip(),
            language=info.language,
            language_probability=float(info.language_probability or 0.0),
        )
    
    def solve(self, sb: SB, identifier: str = "captcha") -> SolveResult:
        """
        Attempt to solve a reCAPTCHA on the current page.
        
        Args:
            sb: SeleniumBase browser instance
            identifier: Unique identifier for naming audio files
            
        Returns:
            SolveResult indicating success/failure
        """
        try:
            # Step 1: Click the reCAPTCHA checkbox
            sb.switch_to_frame(self.RECAPTCHA_IFRAME)
            sb.sleep(1)
            sb.click(self.ANCHOR_SELECTOR)
            sb.switch_to_default_content()
            sb.sleep(2)
            
            # Check if checkbox alone solved it
            if self.is_captcha_solved(sb):
                return SolveResult(success=True, message="Solved with checkbox click")
            
            # Step 2: Switch to audio challenge
            sb.switch_to_frame(self.CHALLENGE_IFRAME)
            sb.click(self.AUDIO_BUTTON)
            sb.sleep(1)
            
            # Step 3: Get audio URL
            audio_url = sb.get_attribute(self.AUDIO_SOURCE, "src")
            if not audio_url:
                sb.switch_to_default_content()
                return SolveResult(success=False, message="Could not find audio source")
            
            # Step 4: Download audio
            audio_file = self._download_audio(sb, audio_url, f"audio-{identifier}.mp3")
            
            # Step 5: Transcribe audio
            result = self._transcribe(audio_file)
            transcription = result.text.strip()
            
            if not transcription:
                if self.cleanup_audio:
                    audio_file.unlink(missing_ok=True)
                sb.switch_to_default_content()
                return SolveResult(success=False, message="Transcription was empty")
            
            # Step 6: Enter transcription and verify
            sb.type(self.AUDIO_RESPONSE, transcription)
            sb.sleep(1)
            sb.click(self.VERIFY_BUTTON)
            
            # Cleanup
            if self.cleanup_audio:
                audio_file.unlink(missing_ok=True)
            
            sb.switch_to_default_content()
            sb.sleep(2)  # Wait longer for page to update
            
            # Check if solved using the new method
            if self.is_captcha_solved(sb):
                return SolveResult(
                    success=True,
                    message="CAPTCHA solved successfully",
                    transcription=transcription
                )
            else:
                return SolveResult(
                    success=False,
                    message="Verification failed",
                    transcription=transcription
                )
                
        except Exception as e:
            try:
                sb.switch_to_default_content()
            except Exception:
                pass
            return SolveResult(success=False, message=f"Error: {str(e)}")
    
    def solve_with_retry(
        self,
        sb: SB,
        identifier: str = "captcha",
        max_attempts: int = 3
    ) -> SolveResult:
        """
        Attempt to solve reCAPTCHA with retries.
        
        Args:
            sb: SeleniumBase browser instance
            identifier: Unique identifier for naming audio files
            max_attempts: Maximum number of solve attempts
            
        Returns:
            SolveResult indicating final success/failure
        """
        for attempt in range(1, max_attempts + 1):
            # First check if captcha is still present
            if not self.is_captcha_present(sb):
                print(f"✅ Captcha already solved (no captcha present)")
                return SolveResult(success=True, message="Captcha already solved")
            
            print(f"🔒 Solve attempt {attempt}/{max_attempts}...")
            result = self.solve(sb, f"{identifier}_{attempt}")
            
            if result.success:
                print(f"✅ {result.message}")
                return result
            
            print(f"❌ Attempt {attempt} failed: {result.message}")
            
            # Check again after attempt - maybe it was solved but verification was wrong
            if self.is_captcha_solved(sb):
                print(f"✅ Captcha appears solved after attempt {attempt}")
                return SolveResult(success=True, message="Captcha solved (verified after attempt)")
            
            if attempt < max_attempts:
                sb.sleep(2)
        
        return SolveResult(
            success=False,
            message=f"Failed after {max_attempts} attempts"
        )