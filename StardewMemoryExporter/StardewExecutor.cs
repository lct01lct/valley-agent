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
using System.Linq;
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
                _cmdServer.Start();
                _monitor.Log($"📡 [DebugServer] TCP 命令接收服务器已在端口 {port} 启动，等待 Python 连接...", LogLevel.Info);

                while (true)
                {
                    try
                    {
                        TcpClient client = _cmdServer.AcceptTcpClient();
                        _connectedClient = client;
                        _netStream = client.GetStream();
                        _monitor.Log("⚙️ [DebugServer] Python 控制端已连接！开始监听网络指令...", LogLevel.Info);

                        byte[] buffer = new byte[4096];
                        string partialData = "";

                        while (_netStream != null && client.Connected)
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

                        _monitor.Log("🔌 [DebugServer] Python 控制端已优雅断开连接。", LogLevel.Warn);
                    }
                    catch (Exception clientEx)
                    {
                        _monitor.Log($"🔌 [DebugServer] Python 客户端异常断开 ({clientEx.Message})，正在重置服务器以等待下次连入...", LogLevel.Warn);
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
                    HandleMove(actionType, pressedKeys);

                }
                else if (actionType.Equals("CLOSE_DIALOG", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("x"))
                {
                    HandleCloseDialog(actionType, pressedKeys);
                }
                else if (actionType.Equals("OPEN_DOOR", StringComparison.OrdinalIgnoreCase) && pressedKeys.Contains("x"))
                {
                    HandleOpenDoor(actionType, pressedKeys);
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

        private void HandleMove(string actionType, List<string> pressedKeys)
        {
            string directionSummary = "";
            if (pressedKeys.Contains("w")) directionSummary += "[上(W)] ";
            if (pressedKeys.Contains("s")) directionSummary += "[下(S)] ";
            if (pressedKeys.Contains("a")) directionSummary += "[左(A)] ";
            if (pressedKeys.Contains("d")) directionSummary += "[右(D)] ";

            if (string.IsNullOrEmpty(directionSummary))
            {
                directionSummary = "[无有效移动键]";
            }

            // _monitor.Log($"🏃 [解析成功] 动作: {actionType} | 拟按下方向: {directionSummary}", LogLevel.Info);

            foreach (string keyStr in pressedKeys)
            {
                if (GetMoveButton(keyStr) is SButton btn)
                {
                    _helper.Input.Press(btn);
                }
            }

            SendResponseToPython("SUCCESS");
            return;
        }

        private void HandleCloseDialog(string actionType, List<string> pressedKeys)
        {
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

        private void HandleOpenDoor(string actionType, List<string> pressedKeys)
        {
            _helper.Input.Press(SButton.X);
            _blackboard.IsWaitingForDoorResponse = true;
            _blackboard.FrameTimeoutCounter = 0;

            return;
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
            try { _netStream?.Close(); _connectedClient?.Close(); _cmdServer?.Stop(); } catch { }
            _netStream = null; _connectedClient = null;
        }
    }
}