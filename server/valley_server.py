import socket
import json
import threading
import time
from typing import cast


class ValleyServer:
    def __init__(self, host="127.0.0.1", port=9999):
        self.host = host
        self.port = port
        self.latest_state = None
        self.lock = threading.Lock()
        self.is_running = False
        self.receiver_thread = None

    def start(self):
        self.is_running = True
        self.receiver_thread = threading.Thread(target=self._network_loop, daemon=True)
        self.receiver_thread.start()
        print("🚀 valley server 实时感知线程已启动，正在监听游戏数据流...")

    def stop(self):
        self.is_running = False
        print("👋 实时感知线程已关闭。")

    def _network_loop(self):
        while self.is_running:
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client.settimeout(5.0)
                client.connect((self.host, self.port))
                print("\n🔌 [Network] 成功连接到星露谷物语内存内核！")

                client.settimeout(None)
                data_accumulator = ""

                while self.is_running:
                    chunk = client.recv(65536).decode("utf-8")
                    if not chunk:
                        print("\n❌ [Network] 游戏断开连接。")
                        break

                    data_accumulator += chunk

                    # 当发现 EOF_END 标志时，切分包
                    while "EOF_END" in data_accumulator:
                        complete_packet, data_accumulator = data_accumulator.split("EOF_END", 1)
                        complete_packet = complete_packet.strip()

                        if not complete_packet:
                            continue

                        try:
                            frame_data = json.loads(complete_packet)
                            px, py = frame_data["player_px"]
                            frame_data["tile_x"] = int(px // 64)
                            frame_data["tile_y"] = int(py // 64)
                            frame_data["clean_obstacles"] = [
                                o.strip() for o in frame_data.get("obstacles", []) if o.strip() and "," in o
                            ]

                            with self.lock:
                                self.latest_state = frame_data

                        except json.JSONDecodeError as je:
                            print(f"\n⚠️ JSON 解析失败，长度 {len(complete_packet)}。原因: {je}")
                            continue

            except (socket.error, ConnectionRefusedError):
                time.sleep(2.0)
            finally:
                try:
                    client.close()
                except:
                    pass

    def get_game_state(self):
        with self.lock:
            return self.latest_state


if __name__ == "__main__":
    from dotenv import load_dotenv
    import os

    load_dotenv(".env")

    agent = ValleyServer(
        cast(str, os.getenv("SMAPI_SEVER_HOST")),
        int(cast(str, os.getenv("SMAPI_SEVER_PORT"))),
    )
    agent.start()

    try:
        while True:
            state = agent.get_game_state()

            if state is None:
                print("⏳ 正在等待游戏内核发送第一帧完整数据包...", end="\r")
                time.sleep(0.2)
                continue

            scene = state["scene_name"]
            tile_x, tile_y = state["tile_x"], state["tile_y"]
            obs_count = len(state["clean_obstacles"])

            print(
                f"🎬 实时场景: {scene:15} | 📍 玩家坐标: ({tile_x:2d}, {tile_y:2d}) | 🧱 障碍物数: {obs_count:4d}",
                end="\r",
            )
            time.sleep(0.1)

    except KeyboardInterrupt:
        agent.stop()
