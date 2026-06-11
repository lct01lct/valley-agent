from langchain_core.messages import HumanMessage
from PIL import Image
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import Optional
from pydantic import BaseModel, Field
from utils.screenshot import image_to_base64


class EnvironmentInfo:
    def __init__(self) -> None:
        pass

    async def get_enviroment_info(
        self,
        vlm_model: ChatGoogleGenerativeAI,
        screenshot: Image.Image,
    ):
        screenshot_base64 = image_to_base64(screenshot)

        query_message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "你是一个经验丰富的星露谷物语玩家，现在你正在一个星露谷物语世界中。请仔细观察这张《星露谷物语》截图，忽略背景画面，专注于提取右上角时间盘、金币栏、右下角能量条以及快捷道具栏等用户界面（UI）中的关键属性和状态，并严格以上述压缩的 JSON 格式输出对应字段，不要包含任何多余的 Markdown 解释或首尾换行。",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"},
                },
            ]
        )

        try:
            structured_vlm = vlm_model.with_structured_output(StardewEnvironmentInfo)
            response: StardewEnvironmentInfo = await structured_vlm.ainvoke([query_message])  # type: ignore
            return response

        except Exception as e:
            raise e


class StardewEnvironmentInfo(BaseModel):
    # 右上角面板
    season: str = Field(description="季节，可选值：春、夏、秋、冬")
    day_of_month: int = Field(description="当前是第几天，通常是 1-28 之间的数字")
    day_of_week: str = Field(description="星期几，例如：星期一、Mon 等")
    time_of_day: str = Field(description="具体时间，例如：9:00 AM, 10:20 PM")

    current_gold: int = Field(description="当前的账户金币总量（纯数字）")

    # 右下角面板
    energy_bar_percentage: int = Field(description="右下角体力条（E）的剩余百分比，0-100 的整数")
    health_bar_percentage: Optional[int] = Field(
        None, description="如果显示了生命值条（H），填其剩余百分比 0-100，未显示则为 None"
    )

    # 工具栏
    active_tool_name: str = Field(description="当前快捷栏中玩家选中的工具或物品名称")

    # 整体界面
    weather: str = Field(description="当前的实时天气，例如：晴天、雨天、雷雨、落花等")
    current_location: str = Field(description="玩家当前身处的地点，例如：农场、家、矿洞、商店，无法判断填未知")


class ObjectPos(BaseModel):
    x: float
    y: float


class TelevisionInfo:
    next_day_weather: str = Field(description="通过电视查看到明天的天气")
    today_luck_level: str = Field(description="通过电视查看到今天的天气")
