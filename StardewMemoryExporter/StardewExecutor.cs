using System;
using System.Collections.Generic;
using System.Linq;
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
using StardewValley.Objects;

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
                else if (actionType.Equals("OPEN_CHEST", StringComparison.OrdinalIgnoreCase))
                {
                    HandleOpenChest(packet);
                }
                else if (actionType.Equals("CLOSE_MENU", StringComparison.OrdinalIgnoreCase))
                {
                    HandleCloseMenu();
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
                else if (actionType.Equals("QUERY_WATER_SOURCES", StringComparison.OrdinalIgnoreCase))
                {
                    string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "Farm";
                    HandleQueryWaterSources(locationName);
                }
                else if (actionType.Equals("QUERY_CHESTS", StringComparison.OrdinalIgnoreCase))
                {
                    string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "Farm";
                    HandleQueryChests(locationName);
                }
                else if (actionType.Equals("TAKE_FROM_CHEST", StringComparison.OrdinalIgnoreCase))
                {
                    HandleTakeFromChest(packet);
                }
                else if (actionType.Equals("TAKE_ITEMS_FROM_CHEST", StringComparison.OrdinalIgnoreCase))
                {
                    HandleTakeItemsFromChest(packet);
                }
                else if (actionType.Equals("PUT_ITEMS_TO_CHEST", StringComparison.OrdinalIgnoreCase))
                {
                    HandlePutItemsToChest(packet);
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

        private void HandleOpenChest(JObject packet)
        {
            ClearHeldMoveButtons();

            if (Game1.activeClickableMenu != null)
            {
                SendResponseToPython("SUCCESS");
                return;
            }

            if (IsPlayerBusyForImmediateCommand())
            {
                SendResponseToPython("BUSY");
                return;
            }

            string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "";
            if (!TryReadTile(packet, out Vector2 chestTile))
            {
                SendResponseToPython("FAILURE");
                return;
            }

            if (Game1.currentLocation == null || !string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
            {
                SendResponseToPython("FAILURE");
                return;
            }

            if (!Game1.currentLocation.Objects.TryGetValue(chestTile, out StardewValley.Object obj) || obj is not Chest)
            {
                SendResponseToPython("FAILURE");
                return;
            }

            if (!IsPlayerCardinalNeighbor(chestTile))
            {
                SendResponseToPython("FAILURE");
                return;
            }

            _helper.Input.Press(SButton.X);
            SendResponseToPython("SUCCESS");
        }

        private void HandleCloseMenu()
        {
            ClearHeldMoveButtons();
            if (Game1.activeClickableMenu == null)
            {
                SendResponseToPython("SUCCESS");
                return;
            }

            Game1.exitActiveMenu();
            SendResponseToPython("SUCCESS");
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

        private void HandleQueryWaterSources(string locationName)
        {
            ClearHeldMoveButtons();

            GameLocation location = Game1.getLocationFromName(locationName);
            if (location == null)
            {
                SendResponseToPython(new JObject
                {
                    ["status"] = "FAILURE",
                    ["reason"] = "LOCATION_NOT_FOUND",
                    ["location_name"] = locationName,
                    ["water_sources"] = new JArray(),
                }.ToString(Newtonsoft.Json.Formatting.None));
                return;
            }

            JArray waterSources = new JArray();
            int mapWidth = location.map.Layers[0].LayerWidth;
            int mapHeight = location.map.Layers[0].LayerHeight;

            for (int x = 0; x < mapWidth; x++)
            {
                for (int y = 0; y < mapHeight; y++)
                {
                    if (location.doesTileHaveProperty(x, y, "Water", "Back") == null)
                    {
                        continue;
                    }

                    waterSources.Add(new JObject
                    {
                        ["Tile"] = new JArray(x, y),
                        ["Source"] = "BackLayerWater",
                    });
                }
            }

            SendResponseToPython(new JObject
            {
                ["status"] = "SUCCESS",
                ["location_name"] = locationName,
                ["water_sources"] = waterSources,
            }.ToString(Newtonsoft.Json.Formatting.None));
        }

        private void HandleTakeFromChest(JObject packet)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForChestTransfer())
            {
                SendChestTransferResponse("FAILURE", "PLAYER_BUSY", "", "", 0, 0, 0, null);
                return;
            }

            string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "";
            string itemName = packet["item_name"]?.ToString() ?? "";
            string qualifiedItemId = packet["qualified_item_id"]?.ToString() ?? "";
            int requestedCount = packet["count"]?.ToObject<int>() ?? 0;

            if (requestedCount <= 0)
            {
                SendChestTransferResponse("FAILURE", "INVALID_COUNT", locationName, itemName, requestedCount, 0, 0, null);
                return;
            }

            if (!TryReadTile(packet, out Vector2 chestTile))
            {
                SendChestTransferResponse("FAILURE", "INVALID_TILE", locationName, itemName, requestedCount, 0, 0, null);
                return;
            }

            if (Game1.currentLocation == null || !string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
            {
                SendChestTransferResponse(
                    "FAILURE",
                    "CURRENT_LOCATION_MISMATCH",
                    locationName,
                    itemName,
                    requestedCount,
                    0,
                    0,
                    chestTile
                );
                return;
            }

            GameLocation location = Game1.currentLocation;
            if (!location.Objects.TryGetValue(chestTile, out StardewValley.Object obj))
            {
                SendChestTransferResponse("FAILURE", "OBJECT_NOT_FOUND", locationName, itemName, requestedCount, 0, 0, chestTile);
                return;
            }

            if (obj is not Chest chest)
            {
                SendChestTransferResponse("FAILURE", "NOT_A_CHEST", locationName, itemName, requestedCount, 0, 0, chestTile);
                return;
            }

            if (!IsPlayerCardinalNeighbor(chestTile))
            {
                SendChestTransferResponse("FAILURE", "PLAYER_NOT_NEXT_TO_CHEST", locationName, itemName, requestedCount, 0, 0, chestTile);
                return;
            }

            int transferredCount = TakeItemsFromChest(chest, itemName, qualifiedItemId, requestedCount, out bool hasMatchingItem);
            string status = transferredCount > 0 ? "SUCCESS" : "FAILURE";
            string reason = transferredCount > 0 ? "" : hasMatchingItem ? "INVENTORY_FULL" : "ITEM_NOT_FOUND";
            SendChestTransferResponse(status, reason, locationName, itemName, requestedCount, transferredCount, CountInventoryItems(itemName, qualifiedItemId), chestTile);
        }

        private void HandleTakeItemsFromChest(JObject packet)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForChestTransfer())
            {
                SendChestBatchTransferResponse("FAILURE", "PLAYER_BUSY", "", null, new JArray());
                return;
            }

            string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "";
            if (!TryReadTile(packet, out Vector2 chestTile))
            {
                SendChestBatchTransferResponse("FAILURE", "INVALID_TILE", locationName, null, new JArray());
                return;
            }

            if (packet["chest_items"] is not JArray chestItems || chestItems.Count == 0)
            {
                SendChestBatchTransferResponse("FAILURE", "INVALID_ITEMS", locationName, chestTile, new JArray());
                return;
            }

            if (Game1.currentLocation == null || !string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
            {
                SendChestBatchTransferResponse("FAILURE", "CURRENT_LOCATION_MISMATCH", locationName, chestTile, new JArray());
                return;
            }

            GameLocation location = Game1.currentLocation;
            if (!location.Objects.TryGetValue(chestTile, out StardewValley.Object obj))
            {
                SendChestBatchTransferResponse("FAILURE", "OBJECT_NOT_FOUND", locationName, chestTile, new JArray());
                return;
            }

            if (obj is not Chest chest)
            {
                SendChestBatchTransferResponse("FAILURE", "NOT_A_CHEST", locationName, chestTile, new JArray());
                return;
            }

            if (!IsPlayerCardinalNeighbor(chestTile))
            {
                SendChestBatchTransferResponse("FAILURE", "PLAYER_NOT_NEXT_TO_CHEST", locationName, chestTile, new JArray());
                return;
            }

            JArray results = new JArray();
            int successCount = 0;
            int transferredItemTypes = 0;

            foreach (JToken rawItemRequest in chestItems)
            {
                string itemName = rawItemRequest["item_name"]?.ToString() ?? "";
                string qualifiedItemId = rawItemRequest["qualified_item_id"]?.ToString() ?? "";
                int requestedCount = rawItemRequest["count"]?.ToObject<int>() ?? 0;

                if (string.IsNullOrWhiteSpace(itemName) || requestedCount <= 0)
                {
                    results.Add(BuildChestItemTransferResult("FAILURE", "INVALID_ITEM_REQUEST", itemName, qualifiedItemId, requestedCount, 0));
                    continue;
                }

                int transferredCount = TakeItemsFromChest(chest, itemName, qualifiedItemId, requestedCount, out bool hasMatchingItem);
                string status = transferredCount >= requestedCount ? "SUCCESS" : "FAILURE";
                string reason = transferredCount >= requestedCount ? "" : hasMatchingItem ? "INVENTORY_FULL" : "ITEM_NOT_FOUND";

                if (transferredCount > 0)
                {
                    transferredItemTypes++;
                }
                if (status == "SUCCESS")
                {
                    successCount++;
                }

                results.Add(BuildChestItemTransferResult(status, reason, itemName, qualifiedItemId, requestedCount, transferredCount));
            }

            string batchStatus = successCount == results.Count ? "SUCCESS" : transferredItemTypes > 0 ? "PARTIAL_SUCCESS" : "FAILURE";
            string batchReason = batchStatus == "SUCCESS" ? "" : "ITEM_TRANSFER_INCOMPLETE";
            SendChestBatchTransferResponse(batchStatus, batchReason, locationName, chestTile, results);
        }

        private void HandlePutItemsToChest(JObject packet)
        {
            ClearHeldMoveButtons();
            if (IsPlayerBusyForChestTransfer())
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "PLAYER_BUSY", "", null, new JArray());
                return;
            }

            string locationName = packet["location_name"]?.ToString() ?? Game1.currentLocation?.Name ?? "";
            if (!TryReadTile(packet, out Vector2 chestTile))
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "INVALID_TILE", locationName, null, new JArray());
                return;
            }

            if (packet["chest_items"] is not JArray chestItems || chestItems.Count == 0)
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "INVALID_ITEMS", locationName, chestTile, new JArray());
                return;
            }

            if (Game1.currentLocation == null || !string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "CURRENT_LOCATION_MISMATCH", locationName, chestTile, new JArray());
                return;
            }

            GameLocation location = Game1.currentLocation;
            if (!location.Objects.TryGetValue(chestTile, out StardewValley.Object obj))
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "OBJECT_NOT_FOUND", locationName, chestTile, new JArray());
                return;
            }

            if (obj is not Chest chest)
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "NOT_A_CHEST", locationName, chestTile, new JArray());
                return;
            }

            if (!IsPlayerCardinalNeighbor(chestTile))
            {
                SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", "FAILURE", "PLAYER_NOT_NEXT_TO_CHEST", locationName, chestTile, new JArray());
                return;
            }

            JArray results = new JArray();
            int successCount = 0;
            int transferredItemTypes = 0;

            foreach (JToken rawItemRequest in chestItems)
            {
                string itemName = rawItemRequest["item_name"]?.ToString() ?? "";
                string qualifiedItemId = rawItemRequest["qualified_item_id"]?.ToString() ?? "";
                int requestedCount = rawItemRequest["count"]?.ToObject<int>() ?? 0;

                if (string.IsNullOrWhiteSpace(itemName) || requestedCount <= 0)
                {
                    results.Add(BuildChestItemTransferResult("FAILURE", "INVALID_ITEM_REQUEST", itemName, qualifiedItemId, requestedCount, 0));
                    continue;
                }

                int transferredCount = PutItemsToChest(chest, itemName, qualifiedItemId, requestedCount, out bool hasMatchingItem, out bool isChestFull);
                string status = transferredCount >= requestedCount ? "SUCCESS" : "FAILURE";
                string reason = "";
                if (status != "SUCCESS")
                {
                    reason = isChestFull ? "CHEST_FULL" : hasMatchingItem ? "INVENTORY_NOT_ENOUGH" : "ITEM_NOT_FOUND";
                }

                if (transferredCount > 0)
                {
                    transferredItemTypes++;
                }
                if (status == "SUCCESS")
                {
                    successCount++;
                }

                results.Add(BuildChestItemTransferResult(status, reason, itemName, qualifiedItemId, requestedCount, transferredCount));
            }

            string batchStatus = successCount == results.Count ? "SUCCESS" : transferredItemTypes > 0 ? "PARTIAL_SUCCESS" : "FAILURE";
            string batchReason = batchStatus == "SUCCESS" ? "" : "ITEM_TRANSFER_INCOMPLETE";
            SendChestBatchTransferResponse("PUT_ITEMS_TO_CHEST", batchStatus, batchReason, locationName, chestTile, results);
        }

        private void HandleQueryChests(string locationName)
        {
            ClearHeldMoveButtons();

            GameLocation location = Game1.getLocationFromName(locationName);
            if (location == null)
            {
                SendResponseToPython(new JObject
                {
                    ["status"] = "FAILURE",
                    ["reason"] = "LOCATION_NOT_FOUND",
                    ["location_name"] = locationName,
                    ["chests"] = new JArray(),
                }.ToString(Newtonsoft.Json.Formatting.None));
                return;
            }

            JArray chests = new JArray();
            foreach (KeyValuePair<Vector2, StardewValley.Object> pair in location.Objects.Pairs)
            {
                if (pair.Value is not Chest chest)
                {
                    continue;
                }

                chests.Add(new JObject
                {
                    ["Tile"] = new JArray((int)pair.Key.X, (int)pair.Key.Y),
                    ["Name"] = chest.Name,
                    ["DisplayName"] = chest.DisplayName,
                    ["ItemCount"] = chest.Items.Count,
                });
            }

            SendResponseToPython(new JObject
            {
                ["status"] = "SUCCESS",
                ["location_name"] = locationName,
                ["chests"] = chests,
            }.ToString(Newtonsoft.Json.Formatting.None));
        }

        private bool TryReadTile(JObject packet, out Vector2 tile)
        {
            tile = Vector2.Zero;
            if (packet["tile"] is not JArray tileArray || tileArray.Count < 2)
            {
                return false;
            }

            tile = new Vector2(tileArray[0]!.ToObject<int>(), tileArray[1]!.ToObject<int>());
            return true;
        }

        private bool IsPlayerCardinalNeighbor(Vector2 targetTile)
        {
            Point playerTile = Game1.player.TilePoint;
            int distanceX = Math.Abs(playerTile.X - (int)targetTile.X);
            int distanceY = Math.Abs(playerTile.Y - (int)targetTile.Y);
            return distanceX + distanceY == 1;
        }

        private int TakeItemsFromChest(
            Chest chest,
            string itemName,
            string qualifiedItemId,
            int requestedCount,
            out bool hasMatchingItem
        )
        {
            int remainingCount = requestedCount;
            int transferredCount = 0;
            hasMatchingItem = false;

            foreach (Item chestItem in chest.Items.ToList())
            {
                if (chestItem == null || !IsItemMatch(chestItem, qualifiedItemId, itemName))
                {
                    continue;
                }
                hasMatchingItem = true;

                int takeCount = Math.Min(remainingCount, chestItem.Stack);
                if (takeCount <= 0)
                {
                    continue;
                }

                Item itemToAdd = chestItem.getOne();
                itemToAdd.Stack = takeCount;
                Item leftover = Game1.player.addItemToInventory(itemToAdd);

                int leftoverCount = leftover?.Stack ?? 0;
                int addedCount = Math.Max(0, takeCount - leftoverCount);
                if (addedCount <= 0)
                {
                    break;
                }

                chestItem.Stack -= addedCount;
                transferredCount += addedCount;
                remainingCount -= addedCount;

                if (chestItem.Stack <= 0)
                {
                    chest.Items.Remove(chestItem);
                }

                if (remainingCount <= 0)
                {
                    break;
                }
            }

            return transferredCount;
        }

        private int PutItemsToChest(
            Chest chest,
            string itemName,
            string qualifiedItemId,
            int requestedCount,
            out bool hasMatchingItem,
            out bool isChestFull
        )
        {
            int remainingCount = requestedCount;
            int transferredCount = 0;
            hasMatchingItem = false;
            isChestFull = false;

            for (int inventoryIndex = 0; inventoryIndex < Game1.player.Items.Count; inventoryIndex++)
            {
                Item inventoryItem = Game1.player.Items[inventoryIndex];
                if (inventoryItem == null || !IsItemMatch(inventoryItem, qualifiedItemId, itemName))
                {
                    continue;
                }
                hasMatchingItem = true;

                int availableCount = Math.Max(inventoryItem.Stack, 1);
                int putCount = Math.Min(remainingCount, availableCount);
                if (putCount <= 0)
                {
                    continue;
                }

                Item itemToPut = inventoryItem.getOne();
                itemToPut.Stack = putCount;
                Item leftover = chest.addItem(itemToPut);

                int leftoverCount = leftover?.Stack ?? 0;
                int addedCount = Math.Max(0, putCount - leftoverCount);
                if (addedCount <= 0)
                {
                    isChestFull = true;
                    break;
                }

                inventoryItem.Stack -= addedCount;
                transferredCount += addedCount;
                remainingCount -= addedCount;

                if (inventoryItem.Stack <= 0)
                {
                    Game1.player.Items[inventoryIndex] = null;
                }

                if (remainingCount <= 0)
                {
                    break;
                }

                if (leftoverCount > 0)
                {
                    isChestFull = true;
                    break;
                }
            }

            return transferredCount;
        }

        private bool IsItemMatch(Item item, string qualifiedItemId, string itemName)
        {
            if (!string.IsNullOrWhiteSpace(qualifiedItemId)
                && string.Equals(item.QualifiedItemId, qualifiedItemId, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (!string.IsNullOrWhiteSpace(itemName)
                && string.Equals(item.Name, itemName, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (!string.IsNullOrWhiteSpace(itemName)
                && string.Equals(item.DisplayName, itemName, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            return false;
        }

        private int CountInventoryItems(string itemName, string qualifiedItemId)
        {
            int totalCount = 0;
            foreach (Item item in Game1.player.Items)
            {
                if (item == null || !IsItemMatch(item, qualifiedItemId, itemName))
                {
                    continue;
                }

                totalCount += Math.Max(item.Stack, 1);
            }

            return totalCount;
        }

        private void SendChestTransferResponse(
            string status,
            string reason,
            string locationName,
            string itemName,
            int requestedCount,
            int transferredCount,
            int inventoryCount,
            Vector2? chestTile
        )
        {
            JObject response = new JObject
            {
                ["status"] = status,
                ["action"] = "TAKE_FROM_CHEST",
                ["reason"] = reason,
                ["location_name"] = locationName,
                ["item_name"] = itemName,
                ["requested_count"] = requestedCount,
                ["transferred_count"] = transferredCount,
                ["inventory_count"] = inventoryCount,
            };

            if (chestTile.HasValue)
            {
                response["tile"] = new JArray((int)chestTile.Value.X, (int)chestTile.Value.Y);
            }

            SendResponseToPython(response.ToString(Newtonsoft.Json.Formatting.None));
        }

        private JObject BuildChestItemTransferResult(
            string status,
            string reason,
            string itemName,
            string qualifiedItemId,
            int requestedCount,
            int transferredCount
        )
        {
            return new JObject
            {
                ["status"] = status,
                ["reason"] = reason,
                ["item_name"] = itemName,
                ["qualified_item_id"] = qualifiedItemId,
                ["requested_count"] = requestedCount,
                ["transferred_count"] = transferredCount,
                ["inventory_count"] = CountInventoryItems(itemName, qualifiedItemId),
            };
        }

        private void SendChestBatchTransferResponse(
            string status,
            string reason,
            string locationName,
            Vector2? chestTile,
            JArray results
        )
        {
            SendChestBatchTransferResponse("TAKE_ITEMS_FROM_CHEST", status, reason, locationName, chestTile, results);
        }

        private void SendChestBatchTransferResponse(
            string action,
            string status,
            string reason,
            string locationName,
            Vector2? chestTile,
            JArray results
        )
        {
            JObject response = new JObject
            {
                ["status"] = status,
                ["action"] = action,
                ["reason"] = reason,
                ["location_name"] = locationName,
                ["results"] = results,
            };

            if (chestTile.HasValue)
            {
                response["tile"] = new JArray((int)chestTile.Value.X, (int)chestTile.Value.Y);
            }

            SendResponseToPython(response.ToString(Newtonsoft.Json.Formatting.None));
        }

        private bool IsPlayerBusyForImmediateCommand()
        {
            Farmer player = Game1.player;
            if (player == null) return true;
            return player.UsingTool || !player.CanMove;
        }

        private bool IsPlayerBusyForChestTransfer()
        {
            Farmer player = Game1.player;
            if (player == null) return true;
            if (player.UsingTool) return true;
            return Game1.activeClickableMenu == null && !player.CanMove;
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
