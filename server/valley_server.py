import socket
import json
import threading
import time
import os
from typing import List, Tuple, Dict, Set, Optional
from PIL import Image, ImageDraw


class WarpZone:
    def __init__(self, target_location: str, tile_x: int, tile_y: int):
        self.target_location: str = target_location
        self.tile_x: int = tile_x
        self.tile_y: int = tile_y


class ValleyState:
    def __init__(self, raw_json_data: dict):
        self.location_name: str = raw_json_data.get("location_name", "UnknownScene")
        self.tile_size: int = raw_json_data.get("tile_size", 64)

        raw_position = raw_json_data.get("position", [0.0, 0.0])
        self.position: Tuple[float, float] = (raw_position[0], raw_position[1])

        tile_coord = raw_json_data.get("tile_coordinate", [0, 0])
        self.player_tile_x: int = tile_coord[0]
        self.player_tile_y: int = tile_coord[1]

        self.warps: List[WarpZone] = []
        for w_dict in raw_json_data.get("warps", []):
            self.warps.append(
                WarpZone(
                    target_location=w_dict.get("target_location", "Unknown"),
                    tile_x=int(w_dict.get("tile_x", 0)),
                    tile_y=int(w_dict.get("tile_y", 0)),
                )
            )

        self.layers: Dict[str, Set[Tuple[int, int]]] = {
            "DEAD": set(),
            "RUG": set(),
            "GRASS": set(),
            "WALL": set(),
            "OBJECT": set(),
            "STONE": set(),
            "BUSH": set(),
            "WORM": set(),
            # 普通树的 6 个阶段
            "T0": set(),
            "T1": set(),
            "T2": set(),
            "T3": set(),
            "T4": set(),
            "T5": set(),
            # 果树的 6 个阶段
            "F0": set(),
            "F1": set(),
            "F2": set(),
            "F3": set(),
            "F4": set(),
            "F5": set(),
        }

        for item in raw_json_data.get("obstacles", []):
            clean_str = item.replace('"', "").strip()
            if ":" in clean_str:
                prefix, coords = clean_str.split(":", 1)
                if "," in coords:
                    try:
                        tx, ty = map(int, coords.split(","))
                        # 🌟 修复的核心：让字典动态匹配所有发送过来的前缀（如 T0, T5, F0 复合标签）
                        if prefix in self.layers:
                            self.layers[prefix].add((tx, ty))
                        elif prefix == "W":
                            self.layers["WALL"].add((tx, ty))
                        elif prefix == "O":
                            self.layers["OBJECT"].add((tx, ty))
                        elif prefix == "S":
                            self.layers["STONE"].add((tx, ty))
                        elif prefix == "B":
                            self.layers["BUSH"].add((tx, ty))
                        elif prefix == "R":
                            self.layers["RUG"].add((tx, ty))
                        elif prefix == "G":
                            self.layers["GRASS"].add((tx, ty))
                        elif prefix == "H":
                            self.layers["WORM"].add((tx, ty))
                        elif prefix == "X":
                            self.layers["DEAD"].add((tx, ty))
                    except ValueError:
                        pass


class StardewPerceptionServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self._latest_state: Optional[ValleyState] = None
        self._lock = threading.Lock()
        self.is_running = False
        self._has_new_data = False

    def start(self):
        self.is_running = True
        threading.Thread(target=self._network_loop, daemon=True).start()
        print("🚀 树木多阶段生命周期优化版视觉雷达已就绪...")

    def _network_loop(self):
        while self.is_running:
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client.settimeout(5.0)
                client.connect((self.host, self.port))
                client.settimeout(None)
                data_accumulator = ""

                while self.is_running:
                    chunk = client.recv(65536).decode("utf-8")
                    if not chunk:
                        break
                    data_accumulator += chunk

                    while "EOF_END" in data_accumulator:
                        complete_packet, data_accumulator = data_accumulator.split("EOF_END", 1)
                        complete_packet = complete_packet.strip()
                        if not complete_packet:
                            continue

                        try:
                            raw_json = json.loads(complete_packet)
                            state_obj = ValleyState(raw_json)
                            with self._lock:
                                self._latest_state = state_obj
                                self._has_new_data = True
                        except json.JSONDecodeError:
                            continue
            except socket.error:
                time.sleep(2.0)
            finally:
                try:
                    client.close()
                except:
                    pass

    def pop_game_state(self) -> Optional[ValleyState]:
        with self._lock:
            if not self._has_new_data:
                return None
            self._has_new_data = False
            return self._latest_state


def render_live_map(state: ValleyState, output_path: str, grid_pixel: int = 40):
    all_points = [(state.player_tile_x, state.player_tile_y)]
    for layer in state.layers.values():
        all_points.extend(layer)

    min_x = min(pt[0] for pt in all_points) - 2
    max_x = max(pt[0] for pt in all_points) + 2
    min_y = min(pt[1] for pt in all_points) - 2
    max_y = max(pt[1] for pt in all_points) + 2

    map_width = max_x - min_x + 1
    map_height = max_y - min_y + 1

    img = Image.new("RGB", (map_width * grid_pixel, map_height * grid_pixel), "#70C15A")
    draw = ImageDraw.Draw(img)

    color_map = {
        "DEAD": "#567A4A",
        "RUG": "#DDA7A5",
        "GRASS": "#A3E04F",
        "WALL": "#30241A",
        "OBJECT": "#B67B50",
        "STONE": "#7A7F85",
        "BUSH": "#317F43",
        "WORM": "#8A5A36",
        # 🌲 普通树（由浅褐色、鹅黄绿逐渐浓郁演化到墨绿大树）
        "T0": "#8B5A2B",  # 0 阶段：埋地里的棕褐色种子
        "T1": "#B3D175",  # 1 阶段：破土浅绿嫩芽
        "T2": "#80B143",  # 2 阶段：小树苗
        "T3": "#4C8A36",  # 3 阶段：中树苗
        "T4": "#2E6B27",  # 4 阶段：即将成熟的紧实小树
        "T5": "#1D5C2E",  # 5 阶段：标准的成熟大树（墨绿色）
        # 🍎 果树（由粉红种子、果树幼苗演化到成熟带有果子特征的深青绿）
        "F0": "#FF6347",  # 0 阶段：亮眼的果树种子/树苗包
        "F1": "#FF8C69",  # 1 阶段
        "F2": "#FFA07A",  # 2 阶段
        "F3": "#CD853F",  # 3 阶段
        "F4": "#4E8B67",  # 4 阶段
        "F5": "#2E5C3E",  # 5 阶段：成熟果树
    }

    # 严格按照“由底到高”的层级顺序渲染，保证大树不会被草地覆盖
    render_order = [
        "DEAD",
        "RUG",
        "GRASS",
        "WALL",
        "OBJECT",
        "STONE",
        "BUSH",
        "WORM",
        "T0",
        "T1",
        "T2",
        "T3",
        "T4",
        "T5",
        "F0",
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
    ]

    for layer_name in render_order:
        color = color_map[layer_name]
        for tx, ty in state.layers[layer_name]:
            cx, cy = tx - min_x, ty - min_y
            if 0 <= cx < map_width and 0 <= cy < map_height:
                x0, y0 = cx * grid_pixel, cy * grid_pixel
                x1, y1 = x0 + grid_pixel - 1, y0 + grid_pixel - 1

                if layer_name == "WORM":
                    draw.rectangle([x0, y0, x1, y1], fill=color)
                    core_margin = int(grid_pixel * 0.25)
                    draw.rectangle(
                        [x0 + core_margin, y0 + core_margin, x1 - core_margin, y1 - core_margin], fill="#E64A19"
                    )
                elif layer_name == "RUG":
                    draw.rectangle([x0, y0, x1, y1], fill=color)
                    draw.rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], outline="#C08080", width=1)

                # 🌟 如果是 0 阶段（不管是普通树种子 T0 还是果树种子 F0）
                elif layer_name in ["T0", "F0"]:
                    # 画成格子核心区域的一个缩进小方格（代表埋在土中心的一个小种子，极其生动）
                    margin = int(grid_pixel * 0.3)
                    draw.rectangle(
                        [x0 + margin, y0 + margin, x1 - margin, y1 - margin], fill=color, outline="#FFFFFF", width=1
                    )

                # 🌟 如果是 1, 2, 3, 4 未成熟的过渡阶段
                elif layer_name in ["T1", "T2", "T3", "T4", "F1", "F2", "F3", "F4"]:
                    # 根据阶段大小画成不同大小的圆，表现出树木在一天天长大！
                    stage_num = int(layer_name[1])
                    circle_margin = int(grid_pixel * (0.4 - stage_num * 0.08))
                    draw.ellipse(
                        [x0 + circle_margin, y0 + circle_margin, x1 - circle_margin, y1 - circle_margin],
                        fill=color,
                        outline="#FFFFFF",
                        width=1,
                    )

                # 其余正常方块（墙体、大树身躯等）
                else:
                    draw.rectangle([x0, y0, x1, y1], fill=color)

    # 3. 传送门大门绘制
    for warp in state.warps:
        cx, cy = warp.tile_x - min_x, warp.tile_y - min_y
        if 0 <= cx < map_width and 0 <= cy < map_height:
            x0, y0 = cx * grid_pixel, cy * grid_pixel
            x1, y1 = x0 + grid_pixel - 1, y0 + grid_pixel - 1

            target = warp.target_location.lower()
            if "greenhouse" in target or "farmhouse" in target or "cabin" in target:
                fill_color = "#FF4500"
                border_color = "#8B0000"
                label_text = "Door"
                if "greenhouse" in target:
                    label_text = "Green"
                if "farmhouse" in target:
                    label_text = "House"
            else:
                fill_color = "#FFD000"
                border_color = "#B08F00"
                label_text = "Warp"

            draw.rectangle([x0, y0, x1, y1], fill=fill_color, outline=border_color, width=2)
            try:
                draw.text((x0 + 4, y0 + grid_pixel // 3), label_text, fill="#FFFFFF")
            except Exception:
                pass

    # 4. 🔴 绘制玩家
    px, py = state.player_tile_x - min_x, state.player_tile_y - min_y
    if 0 <= px < map_width and 0 <= py < map_height:
        x0, y0 = px * grid_pixel, py * grid_pixel
        x1, y1 = x0 + grid_pixel - 1, y0 + grid_pixel - 1
        draw.ellipse([x0 - 2, y0 - 2, x1 + 2, y1 + 2], fill="#FF2E2E", outline="#FFFFFF", width=2)

    temp_img_path = output_path + ".tmp.png"
    img.save(temp_img_path)
    os.replace(temp_img_path, output_path)


if __name__ == "__main__":
    server = StardewPerceptionServer()
    server.start()

    TEXT_FILE = "server/img/stardew_radar.txt"
    IMAGE_FILE = "server/img/stardew_live_map.png"

    try:
        while True:
            state = server.pop_game_state()
            if state is None:
                time.sleep(0.01)
                continue

            # 动态统计地里的总树木与种子树苗数
            total_trees = sum(len(state.layers[f"T{i}"]) + len(state.layers[f"F{i}"]) for i in range(6))
            lines = [
                "================================================================",
                f"🎬 场景 : {state.location_name} | 📍 坐标: ({state.player_tile_x}, {state.player_tile_y})",
                f"🌑 建筑与死墙(WALL): {len(state.layers['WALL'])} | 🌲 全阶段树木总数: {total_trees}",
                f"🟫 刚种下的普通种子(T0): {len(state.layers['T0'])} | 🟥 果树种子树苗(F0): {len(state.layers['F0'])}",
                f"🌿 农场鲜活牧草(GRASS): {len(state.layers['GRASS'])} | 🪱 远古蚯蚓(WORM): {len(state.layers['WORM'])}",
                "================================================================",
            ]
            try:
                with open(TEXT_FILE + ".tmp", "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                os.replace(TEXT_FILE + ".tmp", TEXT_FILE)
            except Exception:
                pass

            try:
                render_live_map(state, IMAGE_FILE, grid_pixel=40)
            except Exception:
                pass

    except KeyboardInterrupt:
        print("\n🏁 服务端已安全退出。")
