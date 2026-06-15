from typing import List, Tuple
from pydantic import BaseModel, Field

path_finding_prompt = """
# Role
你是一个精通《星露谷物语》（Stardew Valley）游戏画面几何空间分析与坐标标定的多模态具身智能（Embodied AI）视觉感知专家。你能把复杂的 2D 像素截图，精准转化为下游 A* 算法和键盘驱动所需的绝对像素数据。

# Task
请仔细观察输入的【游戏实时画面截图】，结合当前已知的游戏状态（地块尺寸 $Scale$），在图像中定位主角、寻找本阶段的目标、并扫描当前视野内阻挡通行的所有障碍物。你的输出将直接作为 A* 算法构建虚拟棋盘（起点、终点、障碍物）的核心基准数据。

注意：必须严格区分“真正的阻挡碰撞体”与“纯视觉背景地皮”，绝不能把可通行、可踩踏的装饰物作为障碍物输出。

# Input Context
- 【当前实时画面】：[用户输入的图像/截图]
- 当前所处场景: `{current_scene}`
- 当前所在位置: `{current_position}`
- 正在前往的下一阶段位置: `{next_position}`
- 上游规划可能行进的方向: `{general_direction}`
- 当前系统精确的地块尺寸 (Scale): `{tile_size}` 像素/格

# CoT (思维链 - 请严格按照以下空间几何逻辑进行推导)
1. 【精确定位玩家角色物理脚底（A* 起点）】：
   - 忽略角色的上半身和头部（因为走路动画会上下晃动）。
   - 在截图中死死锁定角色正下方的【脚底阴影 / 与地面接触的水平面边缘】。
   - 推导：测出该脚底中心点在整张截图中的绝对像素坐标 (X1, Y1)。

2. 【多策略检索寻路终点（A* 终点）】：
   全面扫描截图，寻找 `{next_position}`（如：铁匠铺正门、克林特NPC本体）。
   - 【情况 A：目标在视野内】如果在截图中直接看到了目标，请精准标定该目标“底部与地面交界处”或“交互判定点”的中心，推导其绝对像素坐标为 (X2, Y2)。此时设置 `is_target_in_sight` 为 true。
   - 【情况 B：目标在视野外（盲区探路）】如果截图中没有目标的踪影，立刻根据【可能行进的方向：`{general_direction}`】，在当前截图的最边缘延伸线上标定一个“盲区过渡点”。
     - 示例：若方向为“向右（东）”，请直接在屏幕正右侧边缘、角色正前方的道路延伸处标定一个像素点 (X2, Y2) 作为临时终点。此时设置 `is_target_in_sight` 为 false。

3. 【全面标定不可通行区域（A* 障碍物集合）】：
   仔细观察当前截图中，处于起点 (X1, Y1) 与终点 (X2, Y2) 之间以及周围区域内，所有**绝对无法踩踏通过的实体**。
   - 静态物体：如墙壁、无法穿过的家具、花坛、栅栏、乱石、大树、水体边界。
   - 动态物体：如正在走动的村民 NPC、小镇动物/宠物。
   - 推导：为每一个障碍物拉出一个紧密贴合其物理底座/占地面积的【像素包围框（Bounding Box）】，记录其左上角和右下角像素：[xmin, ymin, xmax, ymax]。
   【物理通过性判定法则】：
   - **绝对黑名单（必须作为障碍物输出）**：墙壁、无法穿过的重型家具（如沙发、桌子、大柜子、壁炉、电视机、前台柜台）、室外障碍（如花坛、栅栏、乱石、大树、房屋边缘、水体边界）、实体村民 NPC。
   - **绝对白名单（纯背景/地皮装饰，绝不能作为障碍物输出）**：**【地毯（Rug）】**、木地板纹理、地砖、各种花纹的地面、农舍内地板上铺设的软垫、散落的小片杂草。玩家和 NPC 可以毫无阻挡地在这些物品上面任意行走！

4. 【生成相对网格步长与障碍验证】：
   利用已知的地块长度 `{tile_size}`，验证起点与终点的相对网格距离：
   - $Delta Tile_x = (X2 - X1) / Scale$
   - $Delta Tile_y = (Y2 - Y1) / Scale$

# Output Format
请严格按照以下 JSON 格式进行结构化输出，不要包含任何 Markdown 代码块标签（如 ```json）、任何多余的解释或 Prose 描述。必须保证输出是一个可以直接被 Python `json.loads()` 解析的纯字符串：

{{
    "player_pixel_coordinate": [`X1`, `Y1`],
    "target_pixel_coordinate": [`X2`, `Y2`],
    "is_target_in_sight": true_or_false,
    "grid_distance_delta": [delta_x, delta_y],
    "obstacles": [
        {{
            "name": "障碍物名称（例如：沙发 / 杂草 / 阿比盖尔）",
            "bounding_box_pixels": [xmin, ymin, xmax, ymax]
        }}
    ]
}}
"""


class Obstacle(BaseModel):
    """视野内单个障碍物的像素级边界框"""

    name: str = Field(description="障碍物的名称或类别，例如：'沙发'、'乱石'、'阿比盖尔'")
    bounding_box_pixels: Tuple[int, int, int, int] = Field(
        description="障碍物物理底座/占地面积的像素包围框，格式为 [xmin, ymin, xmax, ymax]"
    )


class PathFindingOutput(BaseModel):
    player_pixel_coordinate: Tuple[int, int] = Field(
        description="玩家角色物理脚底（与地面接触的水平面边缘中心点）在截图中的绝对像素坐标 [X1, Y1]"
    )
    target_pixel_coordinate: Tuple[int, int] = Field(
        description="目标位置或盲区探路过渡点在截图中的绝对像素坐标 [X2, Y2]"
    )
    is_target_in_sight: bool = Field(
        description="目标是否真正出现在当前视野截图中。若为 false，代表当前 target_pixel_coordinate 仅为朝向指定方向赶路的屏幕边缘盲区过渡点",
    )
    grid_distance_delta: Tuple[float, float] = Field(
        description="根据像素差值除以地块尺寸(Scale)后，算出的相对网格步长 [delta_x, delta_y]"
    )
    obstacles: List[Obstacle] = Field(
        description="当前视野内所有不可通行的静态或动态障碍物列表，供 A* 算法在虚拟棋盘中将其‘涂黑’",
    )


import os
import matplotlib.pyplot as plt
import matplotlib.patches as patches


def draw_path_finding_mock_plot(player_pos, target_pos, obstacles, output_filename):
    """
    根据 VLM 返回的像素坐标数据，生成并保存一张 2D 空间对账草图，用于本地验证 A* 棋盘输入。
    """
    # ================== 🆕 针对 Mac 字体问题的修复 ==================
    # 优先使用 Mac 自带的 'Arial Unicode MS' 或 苹果标准的 'PingFang HK' / 'Heiti TC'
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang HK", "Heiti TC", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False  # 顺便修复负号显示为方块的问题
    # =============================================================

    # 1. 创建画布 (针对 macOS 习惯，深色背景方便对账)
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_facecolor("#2c3e50")

    px, py = player_pos
    overlap_detected = False
    inner_obstacles = []

    # 2. 支持传入 Pydantic 对象或原生字典，进行数据标准化转换
    for obs in obstacles:
        if hasattr(obs, "name") and hasattr(obs, "bounding_box_pixels"):
            name = obs.name
            box = obs.bounding_box_pixels
        else:
            name = obs.get("name", "Unknown")
            box = obs.get("box", obs.get("bounding_box_pixels"))

        inner_obstacles.append({"name": name, "box": box})

    # 3. 遍历并绘制所有障碍物
    for obs in inner_obstacles:
        xmin, ymin, xmax, ymax = obs["box"]

        # 自动纠正大模型可能颠倒的上下/左右物理边界
        x_start, x_end = min(xmin, xmax), max(xmin, xmax)
        y_start, y_end = min(ymin, ymax), max(ymin, ymax)

        width = x_end - x_start
        height = y_end - y_start

        # 核心碰撞检测：判断玩家起点坐标是否一头扎进了当前障碍物内
        if (x_start <= px <= x_end) and (y_start <= py <= y_end):
            edgecolor = "#f1c40f"  # 被卡住时，边框高亮黄色
            facecolor = "#d35400"  # 填充变深橘色
            overlap_detected = True
            print(f"⚠️  [Alignment Alert] 玩家起点 {player_pos} 与障碍物【{obs['name']}】发生重叠！")
        else:
            edgecolor = "#e74c3c"  # 正常障碍物为红色边框
            facecolor = "#c0392b"

        # 在画布上添加障碍物矩形
        rect = patches.Rectangle(
            (x_start, y_start),
            width,
            height,
            linewidth=2,
            edgecolor=edgecolor,
            facecolor=facecolor,
            alpha=0.6,
            label="Obstacle",
        )
        ax.add_patch(rect)

        # 居中写入障碍物名称 (全局字体生效后，这里不需要改动)
        ax.text(
            x_start + width / 2,
            y_start + height / 2,
            obs["name"],
            color="white",
            fontsize=10,
            ha="center",
            va="center",
        )

    # 4. 绘制玩家起点（绿色圆点）
    ax.plot(px, py, marker="o", markersize=12, color="#2ecc71", label="Player Start")
    ax.text(px, py - 15, "玩家(起点)", color="#2ecc71", ha="center", weight="bold")

    # 5. 绘制临时目标点（黄色叉叉）
    ax.plot(target_pos[0], target_pos[1], marker="X", markersize=12, color="#f1c40f", label="Target/Gate")
    ax.text(
        target_pos[0],
        target_pos[1] + 15,
        "目标点(方向盲区)",
        color="#f1c40f",
        ha="center",
        weight="bold",
    )

    # 6. 连线供肉眼评估直线阻挡情况
    ax.plot([px, target_pos[0]], [py, target_pos[1]], color="#f1c40f", linestyle="--", alpha=0.4)

    # 7. 动态调整坐标轴区间（自动包裹所有已知物体并留出 100px 边缘）
    all_x = [px, target_pos[0]] + [b["box"][0] for b in inner_obstacles] + [b["box"][2] for b in inner_obstacles]
    all_y = [py, target_pos[1]] + [b["box"][1] for b in inner_obstacles] + [b["box"][3] for b in inner_obstacles]

    # 适配 macOS 窗口原点习性（反转 Y 轴，左上角为 0,0）
    ax.set_xlim(min(all_x) - 100, max(all_x) + 100)
    ax.set_ylim(max(all_y) + 100, min(all_y) - 100)

    # 8. 图表样式与图例去重
    title_status = " (数据冲突 - 起点被卡死)" if overlap_detected else " (正常)"
    ax.set_title(f"VLM 像素小脑 - 空间物理对账图{title_status}", fontsize=13, color="white", pad=15)
    ax.set_xlabel("X 像素坐标", color="white")
    ax.set_ylabel("Y 像素坐标", color="white")
    ax.tick_params(colors="white")
    ax.grid(True, linestyle=":", alpha=0.2)

    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc="upper left")

    plt.tight_layout()

    # 9. 保存本地并安全释放内存
    plt.savefig(output_filename, dpi=200, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()

    return os.path.abspath(output_filename)
