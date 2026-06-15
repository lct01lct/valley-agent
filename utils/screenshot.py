from PIL import Image
import io
import os
import base64
from io import BytesIO

from Quartz import (
    CGWindowListCopyWindowInfo,  # type: ignore
    kCGWindowListOptionOnScreenOnly,  # type: ignore
    kCGWindowListExcludeDesktopElements,  # type: ignore
    kCGNullWindowID,  # type: ignore
    CGWindowListCreateImage,  # type: ignore
    CGRectMake,  # type: ignore
    kCGWindowListOptionIncludingWindow,  # type: ignore
    kCGWindowImageBoundsIgnoreFraming,  # type: ignore
    kCGWindowImageShouldBeOpaque,  # type: ignore
    CFDataCreateMutable,  # type: ignore
    CGImageDestinationCreateWithData,  # type: ignore
    CGImageDestinationAddImage,  # type: ignore
    CGImageDestinationFinalize,  # type: ignore
    kCGWindowName,  # type: ignore
    kCGWindowOwnerName,  # type: ignore
    kCGWindowBounds,  # type: ignore
    kCGWindowNumber,  # type: ignore
)


def get_window_by_title(window_title):
    window_list = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID
    )

    for window in window_list:
        win_name = window.get(kCGWindowName, "")
        win_owner = window.get(kCGWindowOwnerName, "")

        if window_title in str(win_name) or window_title in str(win_owner):
            bounds = window[kCGWindowBounds]
            x = int(bounds["X"])
            y = int(bounds["Y"])
            width = int(bounds["Width"])
            height = int(bounds["Height"])

            return {"x": x, "y": y, "width": width, "height": height, "id": window[kCGWindowNumber]}
    return None


def capture_specific_window(window_title):
    window = get_window_by_title(window_title)
    if not window:
        raise ValueError(f"未找到标题包含「{window_title}」的窗口！")

    x, y, w, h = window["x"], window["y"], window["width"], window["height"]
    cg_image = CGWindowListCreateImage(
        CGRectMake(x, y, w, h),
        kCGWindowListOptionIncludingWindow,
        window["id"],
        kCGWindowImageBoundsIgnoreFraming | kCGWindowImageShouldBeOpaque,
    )

    if cg_image:
        data = CFDataCreateMutable(None, 0)
        dest = CGImageDestinationCreateWithData(data, "public.png", 1, None)
        CGImageDestinationAddImage(dest, cg_image, None)
        CGImageDestinationFinalize(dest)

        image = Image.open(io.BytesIO(data))
        ori_img_size = image.size
        title_bar_height = int(os.getenv("TITLE_BAR_HEIGHT", 0))

        return image.crop((0, title_bar_height, *ori_img_size))

    else:
        raise ValueError("截图失败！")


def image_to_base64(img_obj: Image.Image, format_name: str = "PNG") -> str:
    buffered = BytesIO()

    img_obj.save(buffered, format=format_name)
    img_bytes = buffered.getvalue()
    base64_encoded = base64.b64encode(img_bytes)

    return base64_encoded.decode("utf-8")


# ===================== 使用示例 =====================
if __name__ == "__main__":
    from dotenv import load_dotenv
    import os
    import time

    load_dotenv(".env")
    start_time = time.perf_counter()

    TARGET_WINDOW = os.getenv("GAME_WINDOW_TITLE")
    image = capture_specific_window(TARGET_WINDOW)
    image.save("assets/images/farm.png")

    cost_time = round(time.perf_counter() - start_time, 4)
    print(f"🔍 截图耗时：{cost_time} 秒")
