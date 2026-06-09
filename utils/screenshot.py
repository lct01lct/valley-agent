from PIL import Image
import io

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
        return image

    else:
        raise ValueError("截图失败！")


# ===================== 使用示例 =====================
if __name__ == "__main__":
    from dotenv import load_dotenv
    import os
    import time

    load_dotenv(".env")
    start_time = time.perf_counter()

    TARGET_WINDOW = os.getenv("GAME_WINDOW_TITLE")
    image = capture_specific_window(TARGET_WINDOW)
    image.save("assets/images/game_full_screenshot_example.png")

    cost_time = round(time.perf_counter() - start_time, 4)
    print(f"🔍 截图耗时：{cost_time} 秒")
