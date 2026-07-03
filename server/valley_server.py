import socket
import json
import threading
import time
import os
from typing import List, Tuple, Dict, Set, Optional
from PIL import Image, ImageDraw

import sys

from agent.action.valley_action.action_type import StardewAction, StardewCommand

sys.path.append("agent")
from agent.action.location.location import Location


class WarpZone:
    def __init__(
        self,
        target_location: str,
        tile_x: int,
        tile_y: int,
        is_passable: bool,
    ):
        self.target_location: str = target_location
        self.tile_x: int = tile_x
        self.tile_y: int = tile_y
        self.is_passable: bool = is_passable


class StardewState:
    def __init__(self, raw_json_data: dict):
        self.location_name: Location = raw_json_data.get("location_name", "UnknownScene")
        self.tile_size: int = raw_json_data.get("tile_size", 0)

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
                    is_passable=bool(w_dict.get("is_passable", False)),
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
            "TREE_STUMP": set(),  # 🌟【与 C# 对齐】：新增硬木大树桩、陨石资源堆图层 (T)
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
                        if prefix in self.layers:
                            self.layers[prefix].add((tx, ty))
                        elif prefix == "W":
                            self.layers["WALL"].add((tx, ty))
                        elif prefix == "T":
                            self.layers["TREE_STUMP"].add((tx, ty))  # 🌟【与 C# 对齐】：解析硬木大树桩 T:x,y
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


class StardewObserverClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self._latest_state: Optional[StardewState] = None
        self._lock = threading.Lock()
        self.is_running = False
        self._has_new_data = False

    def connect(self):
        self.is_running = True
        threading.Thread(target=self._network_loop, daemon=True).start()
        print("🚀 [StardewObserverClient] 已就绪...")

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
                            state_obj = StardewState(raw_json)
                            with self._lock:
                                self._latest_state = state_obj
                                self._has_new_data = True
                        except json.JSONDecodeError:
                            print(1)
                            continue
            except socket.error:
                time.sleep(2.0)
            finally:
                try:
                    client.close()
                except:
                    pass

    def pop_game_state(self) -> Optional[StardewState]:
        with self._lock:
            if not self._has_new_data:
                return None
            self._has_new_data = False
            return self._latest_state


class StardewExecutorClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888):
        self.host = host
        self.port = port
        self.client_socket = None

    def connect(self):
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 默认的 TCP 会为了节省网络带宽，把几个小的数据包攒在一起才发送。
            # 如果不开启这个，Python 发出的指令会被卡在操作系统的缓存区里。
            self.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.client_socket.connect((self.host, self.port))
            print(f"🦿 [StardewExecutorClient] 成功连入 C# 动作控制端 ({self.host}:{self.port})！")
        except Exception as e:
            print(f"❌ [StardewExecutorClient] 连接 C# 失败: {e}，请确保游戏已进入存档且 Mod 已正常运行。")

    def send_command(self, command: StardewCommand):
        if not self.client_socket:
            print("⚠️ [StardewExecutorClient] 未建立网络连接，正在尝试自动重连...")
            self.connect()
            if not self.client_socket:
                return

        try:
            raw_packet = command.model_dump_json() + "\n"
            self.client_socket.sendall(raw_packet.encode("utf-8"))

        except (socket.error, BrokenPipeError):
            print("❌ [StardewExecutorClient] 与游戏的动作控制连接断开！正在尝试重新恢复链路...")
            self.client_socket = None

    def close(self):
        if self.client_socket:
            self.client_socket.close()


def render_live_map(
    state: StardewState,
    output_path: str,
    grid_pixel: int = 40,
    route_list: Optional[List[Tuple[int, int]]] = None,
):
    def extract_route_coords(route_point):
        if isinstance(route_point, dict):
            return int(route_point["x"]), int(route_point["y"])
        return int(route_point[0]), int(route_point[1])

    all_points = [(state.player_tile_x, state.player_tile_y)]
    for layer in state.layers.values():
        all_points.extend(layer)
    if route_list:
        all_points.extend(extract_route_coords(point) for point in route_list)

    min_x = min(pt[0] for pt in all_points) - 2
    max_x = max(pt[0] for pt in all_points) + 2
    min_y = min(pt[1] for pt in all_points) - 2
    max_y = max(pt[1] for pt in all_points) + 2

    map_width = max_x - min_x + 1
    map_height = max_y - min_y + 1

    img = Image.new("RGB", (map_width * grid_pixel, map_height * grid_pixel), "#EBA825")
    draw = ImageDraw.Draw(img)

    color_map = {
        "DEAD": "#07583A",
        "RUG": "#DDA7A5",
        "GRASS": "#A3E04F",
        "WALL": "#30241A",
        "OBJECT": "#64BC26",
        "STONE": "#AB9794",
        "BUSH": "#317F43",
        "WORM": "#8A5A36",
        "TREE_STUMP": "#5C4033",  # 🌟【与 C# 对齐】：深褐色渲染硬木大树桩
        "T0": "#8B5A2B",
        "T1": "#B3D175",
        "T2": "#80B143",
        "T3": "#4C8A36",
        "T4": "#2E6B27",
        "T5": "#1D5C2E",
        "F0": "#FF6347",
        "F1": "#FF8C69",
        "F2": "#FFA07A",
        "F3": "#CD853F",
        "F4": "#4E8B67",
        "F5": "#2E5C3E",
    }
    render_order = [
        "DEAD",
        "RUG",
        "GRASS",
        "WALL",
        "OBJECT",
        "STONE",
        "BUSH",
        "WORM",
        "TREE_STUMP",
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
                elif layer_name == "TREE_STUMP":  # 🌟 给大树桩画个同心内圈，便于一眼在雷达上看出来
                    draw.rectangle([x0, y0, x1, y1], fill=color)
                    draw.rectangle([x0 + 6, y0 + 6, x1 - 6, y1 - 6], outline="#8B4513", width=2)
                elif layer_name in ["T0", "F0"]:
                    margin = int(grid_pixel * 0.3)
                    draw.rectangle(
                        [x0 + margin, y0 + margin, x1 - margin, y1 - margin], fill=color, outline="#FFFFFF", width=1
                    )
                elif layer_name in ["T1", "T2", "T3", "T4", "F1", "F2", "F3", "F4"]:

                    stage_num = int(layer_name[1])
                    circle_margin = int(grid_pixel * (0.4 - stage_num * 0.08))
                    draw.ellipse(
                        [x0 + circle_margin, y0 + circle_margin, x1 - circle_margin, y1 - circle_margin],
                        fill=color,
                        outline="#FFFFFF",
                        width=1,
                    )
                else:
                    draw.rectangle([x0, y0, x1, y1], fill=color)

    for warp in state.warps:
        cx, cy = warp.tile_x - min_x, warp.tile_y - min_y
        if 0 <= cx < map_width and 0 <= cy < map_height:
            x0, y0 = cx * grid_pixel, cy * grid_pixel
            x1, y1 = x0 + grid_pixel - 1, y0 + grid_pixel - 1
            target = warp.target_location.lower()
            fill_color = (
                "#FF4500"
                if any(k in target for k in ["greenhouse", "farmhouse", "cabin"])
                else ("#FFD000" if warp.is_passable else "#6B1D1D")
            )
            border_color = "#8B0000" if fill_color == "#FF4500" else "#B08F00"
            label_text = "House" if "farmhouse" in target else ("Green" if "greenhouse" in target else "Warp")
            draw.rectangle([x0, y0, x1, y1], fill=fill_color, outline=border_color, width=2)
            try:
                draw.text((x0 + 4, y0 + grid_pixel // 3), label_text, fill="#FFFFFF")
            except:
                pass

    if route_list and len(route_list) > 0:
        pixel_points = []
        for route_point in route_list:
            tx, ty = extract_route_coords(route_point)
            cx, cy = tx - min_x, ty - min_y
            center_x = cx * grid_pixel + grid_pixel // 2
            center_y = cy * grid_pixel + grid_pixel // 2
            pixel_points.append((center_x, center_y))

        if len(pixel_points) >= 2:
            draw.line(pixel_points, fill="#42A5F5", width=4, joint="curve")

        end_tx, end_ty = route_list[-1]
        ecx, ecy = end_tx - min_x, end_ty - min_y
        if 0 <= ecx < map_width and 0 <= ecy < map_height:
            ex0, ey0 = ecx * grid_pixel, ecy * grid_pixel
            ex1, ey1 = ex0 + grid_pixel - 1, ey0 + grid_pixel - 1
            draw.rectangle([ex0 + 4, ey0 + 4, ex1 - 4, ey1 - 4], outline="#00E676", width=2)

    px, py = state.player_tile_x - min_x, state.player_tile_y - min_y
    if 0 <= px < map_width and 0 <= py < map_height:
        x0, y0 = px * grid_pixel, py * grid_pixel
        x1, y1 = x0 + grid_pixel - 1, y0 + grid_pixel - 1
        draw.ellipse([x0 - 2, y0 - 2, x1 + 2, y1 + 2], fill="#FF2E2E", outline="#FFFFFF", width=2)

    temp_img_path = output_path + ".tmp.png"
    img.save(temp_img_path)
    os.replace(temp_img_path, output_path)


import threading


def async_render(state_copy, file_path, grid, path):
    try:
        render_live_map(state_copy, file_path, grid, path)
    except Exception:
        pass


if __name__ == "__main__":
    from agent.action.valley_action.AStar import astar_solver

    observer_client = StardewObserverClient()
    observer_client.connect()

    executor_client = StardewExecutorClient(host="127.0.0.1", port=8888)
    executor_client.connect()

    # TEXT_FILE = "server/img/stardew_radar.txt"
    IMAGE_FILE = "server/img/stardew_live_map.png"

    # 持久化记忆路径，彻底阻止 A* 每帧重置污染
    global_current_path = []
    last_location = None

    try:
        while True:
            state = observer_client.pop_game_state()
            if state is None:
                time.sleep(0.01)
                continue

            # total_trees = sum(len(state.layers[f"T{i}"]) + len(state.layers[f"F{i}"]) for i in range(6))
            # lines = [
            #     "================================================================",
            #     f"🎬 场景 : {state.location_name} | 📍 坐标: ({state.player_tile_x}, {state.player_tile_y})",
            #     f"🌑 建筑与死墙(WALL): {len(state.layers['WALL'])} | 🌲 全阶段树木总数: {total_trees} | 🪵 大树桩(STUMP): {len(state.layers['TREE_STUMP'])}",
            #     f"🟫 刚种下的普通种子(T0): {len(state.layers['T0'])} | 🟥 果树种子树苗(F0): {len(state.layers['F0'])}",
            #     f"🌿 农场鲜活牧草(GRASS): {len(state.layers['GRASS'])} | 🪱 远古蚯蚓(WORM): {len(state.layers['WORM'])}",
            #     # f"{','.join(['\"' + warp.target_location + '\"' for warp in state.warps])},",
            #     "================================================================",
            # ]
            # try:
            #     with open(TEXT_FILE + ".tmp", "w", encoding="utf-8") as f:
            #         f.write("\n".join(lines) + "\n")
            #     os.replace(TEXT_FILE + ".tmp", TEXT_FILE)
            # except Exception:
            #     pass

            try:
                target_location_name: Location = "Farm"
                current_loc = state.location_name

                if "FarmHouse" in current_loc:
                    target_location_name = "Farm"
                elif current_loc == "Farm":
                    target_location_name = "BusStop"
                    # target_location_name = "Forest"
                elif current_loc == "BusStop":
                    target_location_name = "Town"
                elif current_loc == "Town":
                    target_location_name = "Blacksmith"
                    # target_location_name = "Forest"
                elif current_loc == "Forest":
                    target_location_name = "Woods"

                # 场景切换时，强行清空路径缓存重寻路
                if current_loc != last_location:
                    global_current_path = []
                    last_location = current_loc

                # 获取当前最新的完整阻挡集合
                # 注意：此处调用 astar_solver 内部的私有阻挡提取方法
                current_blocked_tiles = astar_solver._get_blocked_tiles(state)

                # 【动态提取目标 warp 的状态】
                target_warp_passable = True
                target_warp_tile = None
                for warp in state.warps:
                    if warp.target_location == target_location_name:
                        target_warp_passable = getattr(warp, "is_passable", True)
                        target_warp_tile = (warp.tile_x, warp.tile_y)
                        break

                is_deviated = False
                is_path_blocked = False

                if global_current_path:
                    first_path_tile = astar_solver.get_path_coords(global_current_path[0])
                    # 1. 基础偏航判定：如果当前玩家所处的格子，离路径规划的第一格相差超过 2 个网格，视为严重偏航
                    if (
                        abs(state.player_tile_x - first_path_tile[0]) > 2
                        or abs(state.player_tile_y - first_path_tile[1]) > 2
                    ):
                        is_deviated = True

                    # 2. 动态过期判定（核心修复）：检查缓存路径的未来 3 步之内，是否有格子在最新视野中变成了障碍物
                    # 如果未来要踩雷，说明路径已过期，必须立刻唤醒 A* 动态绕路！
                    look_ahead_steps = min(3, len(global_current_path))
                    for i in range(look_ahead_steps):
                        future_tile = astar_solver.get_path_coords(global_current_path[i])
                        # 如果未来这个格子刚好是不可通行的门，那这本身就是我们规划好的，不视作异常阻挡
                        if future_tile == target_warp_tile and not target_warp_passable:
                            continue
                        if future_tile in current_blocked_tiles:
                            # print(
                            #     f"👁️‍🗨️ [视野更新] 发现已规划的未来格子 {global_current_path[i]} 刷新了障碍物！激活 A* 动态绕路。"
                            # )
                            is_path_blocked = True
                            break

                # 防止到目的地后的空路径无限重算
                # 判定条件：如果路径空了，但我们人其实已经站在不可通行大门前（倒数第二格）了，那就坚决不重复调用 A*
                should_trigger_astar = False
                if not global_current_path:
                    # 如果大门不可通行，且我们已经在门口
                    is_already_at_blocked_door = (not target_warp_passable) and (
                        target_warp_tile is not None
                        and abs(state.player_tile_x - target_warp_tile[0]) <= 1
                        and abs(state.player_tile_y - target_warp_tile[1]) <= 1
                    )
                    if not is_already_at_blocked_door:
                        should_trigger_astar = True
                elif is_deviated or is_path_blocked:
                    should_trigger_astar = True

                # 用于标记这一帧寻路是否陷入了绝路
                is_dead_end = False

                # 如果路径空了、偏航了、或者被新视野下的障碍物堵死了，才允许运行 A*
                if should_trigger_astar:
                    toal_tiles = astar_solver.get_goal_tiles(state, target_location_name)
                    new_path = astar_solver.find_path_to_warp_zone(
                        state, (state.player_tile_x, state.player_tile_y), toal_tiles
                    )

                    # 当发现目标被包裹、被障碍物堵死或无路可走时
                    if new_path is None:
                        if not toal_tiles:
                            print(
                                f"❌ [绝路停机] 无法在当前场景 {state.location_name} 中找到去往目标地点 [{target_location_name}] 的任何传送门！"
                            )
                        else:
                            print(
                                f"⚠️ [绝路停机] 视野内推断出目标 {toal_tiles} 已被障碍物彻底包裹，无法前往！执行紧急切停。"
                            )

                        # 1. 强行清空当前的全局记忆路径，防止继续消费过期的残余路径
                        global_current_path = []
                        # 2. 覆盖当前帧的 command，直接原地大推 IDLE 静止
                        command = StardewCommand(action=StardewAction.IDLE, key=[])
                        is_dead_end = True

                    else:
                        # 过滤试图开倒车的 A* 路径
                        # 如果旧路径已经被控制器推进切短了（比如此时第一格是 3），而新算出来的路径第一格却退回到 4
                        if global_current_path and new_path:
                            if astar_solver.get_path_coords(global_current_path[0]) != astar_solver.get_path_coords(
                                new_path[0]
                            ) and len(new_path) > len(global_current_path):
                                # 判定新路径的下一步是不是在倒退回我们刚刚切掉的那个格子
                                if astar_solver.get_path_coords(new_path[0]) == (
                                    state.player_tile_x,
                                    state.player_tile_y,
                                ) and astar_solver.get_path_coords(new_path[1]) == astar_solver.get_path_coords(
                                    global_current_path[0]
                                ):
                                    # print("🛑 [拦截] 阻挡重算 A* 试图塞回已消费格子，强行抛弃新路径防止原地抽搐！")
                                    new_path = None

                        if new_path is not None:
                            global_current_path = astar_solver.annotate_path_points(
                                new_path,
                                target_warp_passable=target_warp_passable,
                                target_warp_tile=target_warp_tile,
                            )

                # 如果上面 new_path 成功算出来，它会正常走下面的 get_next_move_command
                # 如果上面 new_path 是 None 触发了“绝路停机”，因为 global_current_path 被清空，
                # 下面控制器也会安全返回 IDLE，双重保险保障角色绝对钉在原地不动。
                # 只有在非绝路停机状态下，才允许让控制器去接管驱动逻辑，防止 command 覆盖冲突
                if not is_dead_end:
                    command, global_current_path, require_open_door = astar_solver.get_next_move_command(
                        state=state, current_path=global_current_path, target_warp_passable=target_warp_passable
                    )

                if state.location_name != "Blacksmith":
                    executor_client.send_command(command)
                else:
                    executor_client.send_command(StardewCommand(action=StardewAction.IDLE))

                if "render_thread" not in locals() or not render_thread.is_alive():
                    render_thread = threading.Thread(
                        target=async_render, args=(state, IMAGE_FILE, 40, global_current_path.copy()), daemon=True
                    )
                    render_thread.start()
            except Exception:
                pass

    except KeyboardInterrupt:
        print("\n🏁 [StardewObserverClient] 服务端已安全退出。")
