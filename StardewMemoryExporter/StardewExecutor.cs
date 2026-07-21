using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewModdingAPI.Events;

namespace StardewMemoryExporter
{
    public class StardewExecutor
    {
        private TcpListener _cmdServer;
        private TcpClient _connectedClient;
        private NetworkStream _netStream;
        private readonly IMonitor _monitor;
        private readonly IModHelper _helper;
        private readonly SharedBlackboard _blackboard;
        private readonly object _moveLock = new object();
        private readonly HashSet<SButton> _heldMoveButtons = new HashSet<SButton>();
        private volatile bool _isStopping = false;

        public StardewExecutor(IMonitor monitor, SharedBlackboard blackboard, IModHelper helper, int port = 8888)
        {
            _monitor = monitor;
            _blackboard = blackboard;
            _helper = helper;
            Thread cmdThread = new Thread(() => StartCommandServer(port)) { IsBackground = true };
            cmdThread.Start();
        }

        private void StartCommandServer(int port)
        {
            try
            {
                _cmdServer = new TcpListener(IPAddress.Parse("127.0.0.1"), port);
                _cmdServer.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                _cmdServer.Start();
                _monitor.Log($"📡 [DebugServer] TCP 命令接收服务器已在端口 {port} 启动，等待 Python 连接...", LogLevel.Info);

                while (!_isStopping)
                {
                    try
                    {
                        TcpClient client = _cmdServer.AcceptTcpClient();
                        CloseClientConnection();
                        _connectedClient = client;
                        _netStream = client.GetStream();
                        _monitor.Log("⚙️ [DebugServer] Python 控制端已连接！开始监听网络指令...", LogLevel.Info);

                        byte[] buffer = new byte[4096];
                        string partialData = "";

                        while (!_isStopping && _netStream != null && client.Connected)
                        {
                            int bytesRead = _netStream.Read(buffer, 0, buffer.Length);
                            if (bytesRead == 0) break; // 客户端优雅关闭时会返回 0

                            string data = Encoding.UTF8.GetString(buffer, 0, bytesRead);
                            partialData += data;

                            int lineIndex;
                            // 严格按换行符 \n 拆包，防止网络粘包
                            while ((lineIndex = partialData.IndexOf('\n')) >= 0)
                            {
                                string rawJson = partialData.Substring(0, lineIndex).Trim();
                                partialData = partialData.Substring(lineIndex + 1);

                                if (!string.IsNullOrEmpty(rawJson))
                                {
                                    ParseAndLog(rawJson);
                                }
                            }
                        }

                        ClearHeldMoveButtons();
                        CloseClientConnection();
                        if (!_isStopping)
                        {
                            _monitor.Log("🔌 [DebugServer] Python 控制端已优雅断开连接。", LogLevel.Warn);
                        }
                    }
                    catch (SocketException clientEx)
                    {
                        ClearHeldMoveButtons();
                        CloseClientConnection();
                        if (_isStopping)
                        {
                            _monitor.Log("🛑 [DebugServer] TCP 命令服务器已停止监听。", LogLevel.Trace);
                            break;
                        }

                        _monitor.Log($"🔌 [DebugServer] Python 客户端 Socket 异常断开 ({clientEx.Message})，正在等待下次连入...", LogLevel.Warn);
                    }
                    catch (ObjectDisposedException)
                    {
                        ClearHeldMoveButtons();
                        CloseClientConnection();
                        if (_isStopping)
                        {
                            _monitor.Log("🛑 [DebugServer] TCP 命令服务器资源已释放。", LogLevel.Trace);
                            break;
                        }

                        _monitor.Log("🔌 [DebugServer] Python 客户端连接资源被意外释放，正在等待下次连入...", LogLevel.Warn);
                    }
                    catch (Exception clientEx)
                    {
                        ClearHeldMoveButtons();
                        CloseClientConnection();
                        if (_isStopping)
                        {
                            _monitor.Log("🛑 [DebugServer] TCP 命令服务器已随游戏退出。", LogLevel.Trace);
                            break;
                        }

                        _monitor.Log($"🔌 [DebugServer] Python 客户端异常断开 ({clientEx.Message})，正在等待下次连入...", LogLevel.Warn);
                    }
                    // finally
                    // {
                    //     // 4. 无论如何，哪怕异常了，也必须彻底清理掉死掉的 client 遗留资源，否则下一次 Accept 会报端口占用
                    //     CleanUp();
                    // }

                    // 5. 执行到这里后，外层 while(true) 会自动带代码回到最上方重新执行 AcceptTcpClient()
                }
            }
            catch (Exception rootEx)
            {
                if (_isStopping)
                {
                    _monitor.Log("🛑 [DebugServer] TCP 命令服务器已随游戏退出。", LogLevel.Trace);
                    return;
                }

                // 如果连本地 127.0.0.1 端口都监听失败（比如端口被别的软件抢了），才会执行到这里
                _monitor.Log($"❌ [DebugServer] 发生致命根级崩溃，服务彻底无法启动: {rootEx.Message}", LogLevel.Error);
                CleanUp();
            }
        }
        /// <summary>
        /// 核心调试解析：只读取，只打印，绝不控制角色
        /// </summary>
        private void ParseAndLog(string jsonStr)
        {
            try
            {
                // 1. 打印原始收到的 JSON 字符串
                // _monitor.Log($"📥 [收到原始数据]: {jsonStr}", LogLevel.Debug);

                JObject packet = JObject.Parse(jsonStr);
                string actionType = packet["action"]?.ToString() ?? "IDLE";
                var keysArray = packet["key"] as JArray;
                List<string> pressedKeys = new List<string>();
                if (keysArray != null)
                {
                    foreach (var k in keysArray)
                    {
                        string keyStr = k.ToString().ToLower().Trim();
                        if (!string.IsNullOrEmpty(keyStr)) pressedKeys.Add(keyStr);
                    }
                }

                if (actionType.StartsWith("MOVE", StringComparison.OrdinalIgnoreCase))
                {
                    HandleMove(pressedKeys);

                }
                else if (actionType.Equals("FACE_DIRECTION", StringComparison.OrdinalIgnoreCase))
                {
                    HandleFaceDirection(pressedKeys);
                }
                else if (actionType.Equals("IDLE", StringComparison.OrdinalIgnoreCase))
                {
                    ClearHeldMoveButtons();
                    SendResponseToPython("SUCCESS");
                }
                else if (actionType.Equals("CLOSE_DIALOG", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("x"))
                {
                    HandleCloseDialog(pressedKeys);
                }
                else if (actionType.Equals("OPEN_DOOR", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("x"))
                {
                    HandleOpenDoor(pressedKeys);
                }
                else if (actionType.Equals("USE_TOOL", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("c"))
                {
                    HandleUseTool(pressedKeys);
                }
                else if (actionType.Equals("USE_ITEM", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("x"))
                {
                    HandleUseItem(pressedKeys);
                }
                else if (actionType.Equals("SWITCH_TOOL", StringComparison.OrdinalIgnoreCase))
                {
                    HandleSwitchTool(pressedKeys);
                }

                else
                {
                    SendResponseToPython($"{actionType} | {string.Join(",", pressedKeys)}");
                }


            }
            catch (Exception ex)
            {
                _monitor.Log($"❌ [解析失败] 无法将以下数据反序列化为 JSON: {jsonStr} | 错误: {ex.Message}", LogLevel.Warn);
            }
        }


        private void SendResponseToPython(string status)
        {
            try
            {

                if (_netStream != null && _connectedClient != null && _connectedClient.Connected)
                {
                    byte[] msgBytes = Encoding.UTF8.GetBytes(status + "\n");
                    _netStream.Write(msgBytes, 0, msgBytes.Length);
                    _netStream.Flush();
                }

            }
            catch { }
        }

        private void HandleMove(List<string> pressedKeys)
        {
            if (IsPlayerBusyForImmediateCommand())
            {
                ClearHeldMoveButtons();
                SendResponseToPython("BUSY");
                return;
            }

            string directionSummary = "";
            if (pressedKeys.Contains("w")) directionSummary += "[上(W)] ";
            if (pressedKeys.Contains("s")) directionSummary += "[下(S)] ";
            if (pressedKeys.Contains("a")) directionSummary += "[左(A)] ";
            if (pressedKeys.Contains("d")) directionSummary += "[右(D)] ";

            if (string.IsNullOrEmpty(directionSummary))
            {
                directionSummary = "[无有效移动键]";
            }

            // _monitor.Log($"🏃 [解析成功] 移动方向更新: {directionSummary}", LogLevel.Info);

            HashSet<SButton> nextMoveButtons = new HashSet<SButton>();
            foreach (string keyStr in pressedKeys)
            {
                if (GetMoveButton(keyStr) is SButton btn)
                {
                    nextMoveButtons.Add(btn);
                }
            }

            SetHeldMoveButtons(nextMoveButtons);
            SendResponseToPython("SUCCESS");
            return;
        }

        private void HandleFaceDirection(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForImmediateCommand())
            {
                SendResponseToPython("BUSY");
                return;
            }

            int? facingDirection = GetFacingDirection(pressedKeys);
            if (facingDirection is null)
            {
                SendResponseToPython("FAILURE");
                return;
            }

            Game1.player.faceDirection(facingDirection.Value);
            SendResponseToPython("SUCCESS");
            return;
        }

        private void HandleCloseDialog(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            if (Game1.activeClickableMenu is DialogueBox dialogueBox)
            {
                _helper.Input.Press(SButton.X);
                SendResponseToPython("SUCCESS");
            }
            else
            {
                SendResponseToPython("FAILURE");
            }


            return;
        }

        private void HandleOpenDoor(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            _helper.Input.Press(SButton.X);
            _blackboard.IsWaitingForDoorResponse = true;
            _blackboard.FrameTimeoutCounter = 0;

            return;
        }

        private void HandleUseTool(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForImmediateCommand())
            {
                SendResponseToPython("BUSY");
                return;
            }

            _helper.Input.Press(SButton.C);
            SendResponseToPython("SUCCESS");
            return;
        }

        private void HandleUseItem(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForImmediateCommand())
            {
                SendResponseToPython("BUSY");
                return;
            }

            _helper.Input.Press(SButton.X);
            SendResponseToPython("SUCCESS");
            return;
        }

        private void HandleSwitchTool(List<string> pressedKeys)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForImmediateCommand())
            {
                SendResponseToPython("BUSY");
                return;
            }

            bool hasValidKey = false;
            foreach (string keyStr in pressedKeys)
            {
                if (GetToolSwitchButton(keyStr) is SButton button)
                {
                    _helper.Input.Press(button);
                    hasValidKey = true;
                }
            }

            SendResponseToPython(hasValidKey ? "SUCCESS" : "FAILURE");
            return;
        }

        private bool IsPlayerBusyForImmediateCommand()
        {
            Farmer player = Game1.player;
            if (player == null) return true;
            return player.UsingTool || !player.CanMove;
        }

        private SButton? GetMoveButton(string direction)
        {
            var options = Game1.options;
            return direction.ToLower() switch
            {
                "w" => options.moveUpButton.Length > 0 ? options.moveUpButton[0].ToSButton() : SButton.W,
                "s" => options.moveDownButton.Length > 0 ? options.moveDownButton[0].ToSButton() : SButton.S,
                "a" => options.moveLeftButton.Length > 0 ? options.moveLeftButton[0].ToSButton() : SButton.A,
                "d" => options.moveRightButton.Length > 0 ? options.moveRightButton[0].ToSButton() : SButton.D,
                _ => null
            };
        }

        private int? GetFacingDirection(List<string> pressedKeys)
        {
            if (pressedKeys.Contains("w")) return 0;
            if (pressedKeys.Contains("d")) return 1;
            if (pressedKeys.Contains("s")) return 2;
            if (pressedKeys.Contains("a")) return 3;
            return null;
        }

        private SButton? GetToolSwitchButton(string key)
        {
            return key.ToLower() switch
            {
                "tab" => SButton.Tab,
                "1" => SButton.D1,
                "2" => SButton.D2,
                "3" => SButton.D3,
                "4" => SButton.D4,
                "5" => SButton.D5,
                "6" => SButton.D6,
                "7" => SButton.D7,
                "8" => SButton.D8,
                "9" => SButton.D9,
                "0" => SButton.D0,
                "-" => SButton.OemMinus,
                "=" => SButton.OemPlus,
                _ => null
            };
        }

        private void SetHeldMoveButtons(HashSet<SButton> buttons)
        {
            lock (_moveLock)
            {
                _heldMoveButtons.Clear();
                foreach (SButton button in buttons)
                {
                    _heldMoveButtons.Add(button);
                }
            }
        }

        private void ClearHeldMoveButtons()
        {
            SetHeldMoveButtons(new HashSet<SButton>());
        }

        public void ApplyHeldMove()
        {
            HashSet<SButton> buttonsToHold;
            lock (_moveLock)
            {
                buttonsToHold = new HashSet<SButton>(_heldMoveButtons);
            }

            foreach (SButton button in buttonsToHold)
            {
                _helper.Input.Press(button);
            }
        }

        public void ListenDialogMessages(UpdateTickedEventArgs e)
        {
            if (_blackboard.IsWaitingForDoorResponse)
            {
                _blackboard.FrameTimeoutCounter++;

                if (Game1.activeClickableMenu is DialogueBox dialogueBox)
                {
                    string dialogueText = dialogueBox.getCurrentString();

                    SendResponseToPython(!string.IsNullOrEmpty(dialogueText) ? dialogueText : "SUCCESS");

                    _blackboard.IsWaitingForDoorResponse = false;
                    _blackboard.FrameTimeoutCounter = 0;
                }

                else if (_blackboard.FrameTimeoutCounter > 5)
                {
                    SendResponseToPython("TIMEOUT");
                    _blackboard.IsWaitingForDoorResponse = false;
                    _blackboard.FrameTimeoutCounter = 0;
                }
            }


        }

        public void CleanUp()
        {
            _isStopping = true;
            ClearHeldMoveButtons();
            CloseClientConnection();

            try
            {
                _cmdServer?.Stop();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [DebugServer] 停止 TCP 命令监听器时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            _cmdServer = null;
        }

        private void CloseClientConnection()
        {
            try
            {
                _netStream?.Close();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [DebugServer] 关闭命令数据流时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            try
            {
                _connectedClient?.Close();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [DebugServer] 关闭命令客户端时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            _netStream = null;
            _connectedClient = null;
        }
    }
}
