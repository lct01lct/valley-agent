from typing import List, Tuple
from pydantic import BaseModel, Field

path_finding_prompt = """
# Role
你是一个精通《星露谷物语》（Stardew Valley）游戏画面几何空间分析与碰撞体标定的多模态具身智能（Embodied AI）视觉感知专家。你能把复杂的 2D 游戏实时画面，精准转化为下游 A* 算法和键盘驱动所需的标准化归一化空间坐标。

# Task
请仔细观察输入的【游戏实时画面截图】，结合当前已知的游戏状态，在图像中定位主角、寻找本阶段的目标、并扫描当前视野内阻挡通行的“硬性障碍物”。

⚠️ 【核心原则】：
1. 必须使用 [0, 1000] 的【千分比归一化相对坐标系】来标定一切空间位置，严禁直接猜测绝对像素。
2. 必须严格区分“真正的阻挡碰撞体”与“纯视觉背景地皮”，绝不能把可通行、可踩踏的装饰物作为障碍物输出。

# Input Context
- 当前所处场景: `{current_scene}`
- 当前所在位置: `{start_position}`
- 正在前往的下一阶段位置: `{end_position}`
- 上游规划可能行进的方向: `{general_direction}`
- 当前系统精确的地块尺寸 (Scale): `{tile_size}` 像素/格

# CoT (思维链 - 请严格按照以下空间几何与归一化逻辑进行推导)
1. 【建立千分比归一化坐标系】：
   - 将当前输入的整个截图视作一个固定的 1000 x 1000 网格画布。
   - 规定：左上角顶点坐标为 [0, 0]，右上角为 [1000, 0]，左下角为 [0, 1000]，右下角为 [1000, 1000]。

2. 【精确定位玩家角色物理脚底（A* 起点）】：
   - 忽略角色的上半身和头部（因为走路动画会随帧上下晃动）。
   - 在截图中死死锁定角色正下方的【脚底黑色椭圆阴影 / 与地面接触的水平面边缘】。
   - 推导：测出该脚底中心点在 1000x1000 画布中的比例位置，记为归一化坐标 [nX1, nY1]。

3. 【多策略检索寻路终点（A* 终点）】：
   全面扫描截图，寻找 `{end_position}`（如：铁匠铺正门、克林特NPC本体、特定出口）。
   - 【情况 A：目标在视野内】如果在截图中直接看到了目标，请精准标定该目标“底部与地面交界处”或“交互判定点”的中心，推导其在 1000x1000 画布中的归一化坐标为 [nX2, nY2]。此时设置 `is_target_in_sight` 为 true。
   - 【情况 B：目标在视野外（盲区探路）】如果截图中没有目标的踪影，立刻根据【可能行进的方向：`{general_direction}`】，在当前截图的最边缘延伸线上标定一个“盲区过渡点”。
     - 示例：若方向为“向右（东）”，请直接在屏幕正右侧边缘（nX2=1000）、角色正前方的道路延伸处标定一个坐标点 [nX2, nY2] 作为临时终点。此时设置 `is_target_in_sight` 为 false。
    
    特别注意（边缘临界判定）：若 end_position 是出口、门廊、转场大门，且该大门的任何一部分（如上半部分门框、地毯边缘）已经暴露在屏幕最下方或边缘切线上，哪怕没有看到门外的世界，也必须将其视为【在视野内】（is_target_in_sight 设为 true），并直接将终点坐标标定在该暴露出的门洞中心。

4. 【全面标定硬性障碍物区域（A* 障碍物集合）】：
   仔细观察当前截图中，处于起点 [nX1, nY1] 与终点 [nX2, nY2] 之间以及周围区域内，所有**绝对无法踩踏通过的垂直碰撞实体**。
   
   ⚠️ 【物理通过性判定法则】：
   - **绝对黑名单（必须作为障碍物输出）**：墙壁（北墙、西墙等边界）、无法穿过的重型家具（如沙发、桌子、大柜子、壁炉、电视机、前台柜台）、室外障碍（如花坛、栅栏、乱石、大树、房屋边缘、水体边界）、实体村民 NPC。
   - **绝对白名单（纯背景/地皮装饰，绝不能作为障碍物输出）**：**【地毯（Rug）】**、木地板纹理、地砖、各种花纹的地面、农舍内地板上铺设的软垫、散落的小片杂草。玩家和 NPC 可以毫无阻挡地在这些物品上面任意行走！
   
   - 推导：过滤掉白名单物体后，为每一个硬性障碍物拉出一个紧密贴合其物理底座/占地面积的【归一化包围框（Bounding Box）】，记录其左上角和右下角在 1000x1000 画布中的比例值：[nxmin, nymin, nxmax, nymax]。

5. 【防自碰撞锁死自检（Flash模型关键纠偏）】：
   - 检查：玩家当前的脚底位置 [nX1, nY1] 是否不小心落在了你刚才标定的任意一个障碍物的 [nxmin, nymin, nxmax, nymax] 范围内部？
   - 修正：如果发现重叠（例如标定床铺时把玩家也圈进去了），必须立刻微调该障碍物或玩家的边界，**确保玩家起点 [nX1, nY1] 在物理上完全处于空地上**，绝对不能与任何障碍物重叠！

# Output Format
请严格按照以下 JSON 格式进行结构化输出，不要包含任何 Markdown 代码块标签（如 ```json）、任何多余的解释或 Prose 描述。必须保证输出是一个可以直接被 Python `json.loads()` 解析的纯字符串。所有坐标数字必须是 0 到 1000 之间的整数：

{{
    "player_normalized_coordinate": [nX1, nY1],
    "target_normalized_coordinate": [nX2, nY2],
    "is_target_in_sight": true_or_false,
    transition_point_position: None_or_str,
    "obstacles": [
        {{
            "name": "硬性障碍物名称（例如：沙发 / 乱石 / 阿比盖尔）",
            "normalized_bounding_box": [nxmin, nymin, nxmax, nymax]
        }}
    ]
}}
"""


class Obstacle(BaseModel):
    """视野内单个阻挡碰撞体的归一化空间边界"""

    name: str = Field(
        description="硬性障碍物的名称或类别，例如：'电视机'、'乱石'、'阿比盖尔'（绝对白名单如地毯、木地板纹理禁填）"
    )
    normalized_bounding_box: Tuple[int, int, int, int] = Field(
        description="障碍物物理底座占地面积在 1000x1000 虚拟画布中的归一化包围框，格式为 [nxmin, nymin, nxmax, nymax]，数值均在 0-1000 之间"
    )


class PathFindingOutput(BaseModel):
    player_normalized_coordinate: Tuple[int, int] = Field(
        description="玩家角色物理脚底（黑色椭圆阴影中心）在 1000x1000 虚拟画布中的归一化比例坐标 [nX1, nY1]，数值在 0-1000 之间",
    )
    target_normalized_coordinate: Tuple[int, int] = Field(
        description="目标位置或盲区赶路延伸过渡点在 1000x1000 虚拟画布中的归一化比例坐标 [nX2, nY2]，数值在 0-1000 之间",
    )
    is_target_in_sight: bool = Field(
        description="end_position 是否真正出现在当前视野截图中。若为 false，代表当前坐标仅为朝向 general_direction 赶路的屏幕边缘过渡点",
    )
    transition_point_position: str | None = Field("如果 end_position 不在视野中，则清晰描述过渡点，否则返回 None")
    obstacles: List[Obstacle] = Field(
        default_factory=list,
        description="当前视野内所有不可通行的硬性静态或动态障碍物列表（自动排除白名单地毯等），供 A* 算法构建棋盘使用",
    )


import os
import json
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.ticker as ticker


def draw_path_finding_mock_plot(vlm_output, image_width, image_height, tile_size, output_filename):
    """
    根据 Gemini-Flash 返回的【归一化比例数据】，自动将其还原为真实像素，并绘制带游戏网格的 2D 空间对账图。

    :param vlm_output: 可以是 PathFindingOutput Pydantic 对象，也可以是对应的 dict/JSON 字符串
    :param image_width: 经过裁剪后的游戏画面真实宽度（像素）
    :param image_height: 经过裁剪后的游戏画面真实高度（像素）
    :param tile_size: 当前地块尺寸 Scale (像素/格，如 64)
    :param output_filename: 本地图片保存路径
    :return: str 保存成功后的绝对路径
    """
    # 针对 Mac 字体与负号问题的修复
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang HK", "Heiti TC", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    # 1. 解析数据源 (兼容 Pydantic 对象、dict 或原生 JSON 字符串)
    if isinstance(vlm_output, str):
        try:
            data = json.loads(vlm_output)
        except json.JSONDecodeError:
            raise ValueError("输入的 vlm_output 无法被解析为 JSON 字符串。")
    elif hasattr(vlm_output, "model_dump"):  # Pydantic v2
        data = vlm_output.model_dump()
    elif hasattr(vlm_output, "dict"):  # Pydantic v1
        data = vlm_output.dict()
    else:
        data = vlm_output

    # 2. 内部比例恢复像素的工具函数
    def to_pixel(nx, ny):
        return int((nx / 1000.0) * image_width), int((ny / 1000.0) * image_height)

    # 还原起点和终点像素
    px, py = to_pixel(*data.get("player_normalized_coordinate"))
    tx, ty = to_pixel(*data.get("target_normalized_coordinate"))
    is_target_in_sight = data.get("is_target_in_sight", False)

    # 3. 创建画布
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_facecolor("#2c3e50")  # 经典暗灰蓝背景

    overlap_detected = False
    real_obstacles = []

    # 4. 遍历并绘制所有障碍物
    obstacles_list = data.get("obstacles", [])
    for obs in obstacles_list:
        name = obs.get("name", "Unknown")
        box = obs.get("normalized_bounding_box", obs.get("box"))
        if not box or len(box) != 4:
            continue

        nx1, ny1, nx2, ny2 = box

        # 将归一化千分比映射回真实的像素坐标
        xmin, ymin = to_pixel(nx1, ny1)
        xmax, ymax = to_pixel(nx2, ny2)

        # 自动纠正大模型可能颠倒的上下/左右物理边界
        x_start, x_end = min(xmin, xmax), max(xmin, xmax)
        y_start, y_end = min(ymin, ymax), max(ymin, ymax)

        width = x_end - x_start
        height = y_end - y_start

        # 核心碰撞检测：如果 Flash 模型产生幻觉把玩家脚底扣进去了，变色高亮
        if (x_start <= px <= x_end) and (y_start <= py <= y_end):
            edgecolor = "#f1c40f"  # 自锁冲突：边框黄色
            facecolor = "#d35400"  # 填充橘色
            overlap_detected = True
            print(f"⚠️  [Alignment Alert] 恢复像素后，玩家起点 ({px}, {py}) 与障碍物【{name}】依然发生碰撞！")
        else:
            edgecolor = "#e74c3c"  # 正常障碍物：红色
            facecolor = "#c0392b"

        # 添加障碍物矩形
        rect = patches.Rectangle(
            (x_start, y_start),
            width,
            height,
            linewidth=1.5,
            edgecolor=edgecolor,
            facecolor=facecolor,
            alpha=0.55,
            label="Obstacle",
        )
        ax.add_patch(rect)

        # 居中写入障碍物名称
        ax.text(x_start + width / 2, y_start + height / 2, name, color="white", fontsize=9, ha="center", va="center")
        real_obstacles.append({"name": name, "box": (x_start, y_start, x_end, y_end)})

    # 5. 绘制玩家起点（绿色圆点）
    ax.plot(px, py, marker="o", markersize=12, color="#2ecc71", label="Player Start")
    ax.text(px, py - 18, f"玩家起点\n({px},{py})", color="#2ecc71", fontsize=9, ha="center", weight="bold")

    # 6. 绘制目标点（黄色叉叉）
    target_label = "目标点(可见)" if is_target_in_sight else "目标点(盲区过渡)"
    ax.plot(tx, ty, marker="X", markersize=12, color="#f1c40f", label="Target/Gate")
    ax.text(tx, ty + 18, f"{target_label}\n({tx},{ty})", color="#f1c40f", fontsize=9, ha="center", weight="bold")

    # 7. 连线供评估
    ax.plot([px, tx], [py, ty], color="#f1c40f", linestyle="--", alpha=0.4)

    # 8. 强力杀手锏功能：根据游戏真实 Scale 自动绘制网格线
    # 让网格线对齐到整数个格子，方便你数格子对账
    ax.xaxis.set_major_locator(ticker.MultipleLocator(tile_size))
    ax.yaxis.set_major_locator(ticker.MultipleLocator(tile_size))
    ax.grid(True, which="major", color="#34495e", linestyle="-", linewidth=0.8, alpha=0.6)

    # 9. 动态调整坐标轴区间（自动包裹所有物体，并向外延伸 1.5 个格子）
    all_x = [px, tx] + [b["box"][0] for b in real_obstacles] + [b["box"][2] for b in real_obstacles]
    all_y = [py, ty] + [b["box"][1] for b in real_obstacles] + [b["box"][3] for b in real_obstacles]

    padding = int(tile_size * 1.5)

    # macOS 窗口原点习性（反转 Y 轴，左上角为 0,0）
    ax.set_xlim(min(all_x) - padding, max(all_x) + padding)
    ax.set_ylim(max(all_y) + padding, min(all_y) - padding)

    # 10. 样式修饰与图例去重
    title_status = " (数据冲突 - 起点自锁)" if overlap_detected else " (正常)"
    ax.set_title(
        f"VLM 像素小脑 - 空间物理对账图{title_status} [Scale={tile_size}px]", fontsize=13, color="white", pad=15
    )
    ax.set_xlabel("X 像素轴 (横向格线间隔=1格)", color="#bdc3c7")
    ax.set_ylabel("Y 像素轴 (纵向格线间隔=1格)", color="#bdc3c7")
    ax.tick_params(colors="white", labelsize=8)

    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc="upper left", framealpha=0.4)

    plt.tight_layout()

    # 11. 保存并释放内存
    plt.savefig(output_filename, dpi=200, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()

    return os.path.abspath(output_filename)
