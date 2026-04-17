import numpy as np
from scipy.ndimage import laplace

_CROP = 200  # half-size -> 400x400 centre crop


def compute_focus_score(rgb: np.ndarray) -> float:
    """Laplacian variance of the centre 400x400 region. Higher = sharper."""
    h, w = rgb.shape[:2]
    cy, cx = h // 2, w // 2
    crop = rgb[cy - _CROP: cy + _CROP, cx - _CROP: cx + _CROP]
    gray = np.dot(crop[..., :3].astype(np.float64), [0.299, 0.587, 0.114])
    return float(laplace(gray).var())
