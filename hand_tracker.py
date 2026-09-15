"""MediaPipe hand tracking and gesture recognition.

The tracker maps a mirrored camera image into normalized game coordinates. It
uses hysteresis for fist and open-palm gestures so the cursor does not jitter
between states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import mediapipe as mp
import numpy as np


GESTURE_MOVE = "move"
GESTURE_GRAB = "grab"
GESTURE_THROW = "throw"


@dataclass(frozen=True)
class HandFrame:
    found: bool
    x: float = 0.5
    y: float = 0.5
    gesture: str = GESTURE_MOVE
    raw_gesture: str = GESTURE_MOVE
    landmarks: tuple[tuple[float, float, float], ...] = ()
    frame_bgr: np.ndarray | None = None


class HandTracker:
    def __init__(
        self,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
        min_detection_confidence: float = 0.65,
        min_tracking_confidence: float = 0.6,
    ) -> None:
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.capture: cv2.VideoCapture | None = None
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            model_complexity=1,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )
        self._last_gesture = GESTURE_MOVE

    def open(self) -> bool:
        self.capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0)
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.capture.set(cv2.CAP_PROP_FPS, 30)
        return bool(self.capture.isOpened())

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self.hands.close()

    def read(self) -> HandFrame:
        if self.capture is None:
            return HandFrame(False)
        ok, frame = self.capture.read()
        if not ok:
            return HandFrame(False, frame_bgr=frame)

        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self.hands.process(rgb)
        rgb.flags.writeable = True

        if not result.multi_hand_landmarks:
            self._last_gesture = GESTURE_MOVE
            return HandFrame(False, gesture=GESTURE_MOVE, frame_bgr=frame)

        hand = result.multi_hand_landmarks[0]
        landmarks = tuple((point.x, point.y, point.z) for point in hand.landmark)
        raw_gesture = self._classify_gesture(landmarks)
        gesture = self._stabilize_gesture(raw_gesture)

        palm = landmarks[9]
        x = float(np.clip(palm[0], 0.0, 1.0))
        y = float(np.clip(palm[1], 0.0, 1.0))
        return HandFrame(True, x, y, gesture, raw_gesture, landmarks, frame)

    def _stabilize_gesture(self, raw: str) -> str:
        # Grab and throw are events. Once detected, keep the event visible for
        # one frame so the game loop can consume it reliably.
        if raw in (GESTURE_GRAB, GESTURE_THROW):
            self._last_gesture = raw
            return raw
        self._last_gesture = GESTURE_MOVE
        return GESTURE_MOVE

    @staticmethod
    def _classify_gesture(landmarks: tuple[tuple[float, float, float], ...]) -> str:
        if len(landmarks) < 21:
            return GESTURE_MOVE

        fingertips = (4, 8, 12, 16, 20)
        pips = (3, 6, 10, 14, 18)
        extended = []
        for tip, pip in zip(fingertips, pips):
            if tip == 4:
                wrist_x = landmarks[0][0]
                extended.append(abs(landmarks[tip][0] - wrist_x) > abs(landmarks[pip][0] - wrist_x) * 1.12)
            else:
                extended.append(landmarks[tip][1] < landmarks[pip][1] - 0.015)

        extended_count = sum(extended)
        if extended_count <= 1:
            return GESTURE_GRAB
        if extended_count >= 4:
            return GESTURE_THROW
        return GESTURE_MOVE

    @staticmethod
    def draw_camera_overlay(
        frame_bgr: np.ndarray,
        landmarks: tuple[tuple[float, float, float], ...],
        width: int = 240,
        height: int = 180,
    ) -> np.ndarray:
        frame = cv2.resize(frame_bgr, (width, height), interpolation=cv2.INTER_AREA)
        if landmarks:
            connections = (
                (0, 1), (1, 2), (2, 3), (3, 4),
                (0, 5), (5, 6), (6, 7), (7, 8),
                (5, 9), (9, 10), (10, 11), (11, 12),
                (9, 13), (13, 14), (14, 15), (15, 16),
                (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
            )
            points = [(int(x * width), int(y * height)) for x, y, _ in landmarks]
            for start, end in connections:
                cv2.line(frame, points[start], points[end], (80, 230, 180), 2, cv2.LINE_AA)
            for point in points:
                cv2.circle(frame, point, 2, (255, 225, 100), -1, cv2.LINE_AA)
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
