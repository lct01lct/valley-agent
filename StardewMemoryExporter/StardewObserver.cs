using System;
using System.IO;
using System.Text;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Collections.Generic;
using System.Linq;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Objects;
using StardewValley.Locations;
using StardewValley.TerrainFeatures;
using StardewValley.Buildings;
using Microsoft.Xna.Framework;
using Newtonsoft.Json;

namespace StardewMemoryExporter
{
    public class StardewObserver
    {
        private TcpListener _server;
        private TcpClient _connectedClient;
        private NetworkStream _netStream;
        private readonly object _streamLock = new object();
        private readonly IMonitor _monitor;

        private string _lastCachedLocation = "";
        private readonly List<string> _cachedWarpJsonList = new List<string>();
        private readonly HashSet<string> _cachedWarpCoords = new HashSet<string>();

        // 🛡️ 调试专用变量：记录上一次打印的玩家格子坐标，防止高频刷屏
        private Vector2 _lastLoggedPlayerTile = Vector2.Zero;

        public StardewObserver(IMonitor monitor, int port = 9999)
        {
            _monitor = monitor;
            Thread serverThread = new Thread(() => StartTcpServer(port)) { IsBackground = true };
            serverThread.Start();
        }

        private void StartTcpServer(int port)
        {
            try
            {
                _server = new TcpListener(IPAddress.Parse("127.0.0.1"), port);
                _server.Start();
                while (true)
                {
                    TcpClient client = _server.AcceptTcpClient();
                    lock (_streamLock)
                    {
                        CleanUp();
                        _connectedClient = client;
                        _netStream = client.GetStream();
                    }
                    _monitor.Log("📡 [StardewObserver] Python 大脑已成功连入场景雷达数据流！", LogLevel.Info);
                }
            }
            catch (Exception ex)
            {
                _monitor.Log($"❌ [StardewObserver] 雷达服务器崩溃: {ex.Message}", LogLevel.Error);
            }
        }

        public void PulseGameMemory()
        {
            if (_netStream == null || !Context.IsWorldReady || Game1.currentLocation == null) return;

            try
            {
                var location = Game1.currentLocation;
                var player = Game1.player;

                // ========================================================================
                // 🔍 🌟 调试：预判当前脚下踩着的格子
                // ========================================================================
                // Vector2 currentTile = new Vector2((int)player.TilePoint.X, (int)player.TilePoint.Y);

                // if (currentTile != _lastLoggedPlayerTile)
                // {
                //     _lastLoggedPlayerTile = currentTile;
                //     int px = (int)currentTile.X;
                //     int py = (int)currentTile.Y;

                //     string finalJsonString = "🟩 完美通行区 (完全不在 obstacles 列表中，Python 端可百分之百寻路)";

                //     // 1. 检查建筑物理碰撞（内部已完美豁免大门）
                //     bool blockedByBuilding = false;
                //     if (location is Farm farmObj)
                //     {
                //         foreach (var building in farmObj.buildings)
                //         {
                //             if (building != null && IsTileBlockedByBuilding(building, currentTile))
                //             {
                //                 blockedByBuilding = true;
                //                 break;
                //             }
                //         }
                //     }

                //     if (blockedByBuilding)
                //     {
                //         finalJsonString = $"\"W:{px},{py}\" 🛑 (被建筑实体硬阻挡)";
                //     }
                //     // 2. 基础地图图层物理碰撞
                //     else if (!location.isTilePassable(new xTile.Dimensions.Location(px, py), Game1.viewport))
                //     {
                //         finalJsonString = $"\"W:{px},{py}\" 🛑 (基础地图硬墙体/无法通行瓷砖)";
                //     }
                //     // 3. 普通物品
                //     else if (location.Objects.TryGetValue(currentTile, out StardewValley.Object debugObj) && debugObj != null)
                //     {
                //         if (debugObj.ParentSheetIndex == 590 || (debugObj.Name != null && debugObj.Name.Contains("Artifact Spot")))
                //             finalJsonString = $"\"H:{px},{py}\" 🏺 (远古斑点阻挡)";
                //         else if (debugObj.Name != null && debugObj.Name.Contains("Stone"))
                //             finalJsonString = $"\"S:{px},{py}\" 🪨 (石头阻挡)";
                //         else
                //             finalJsonString = $"\"O:{px},{py}\" 📦 (普通物品阻挡)";
                //     }
                //     // 4. 地形特征
                //     else if (location.terrainFeatures.TryGetValue(currentTile, out var debugFeature) && debugFeature != null)
                //     {
                //         if (debugFeature is Tree ordinaryTree) finalJsonString = $"\"T{ordinaryTree.growthStage.Value}:{px},{py}\" 🌲 (树木阻挡)";
                //         else if (debugFeature is FruitTree fruitTree) finalJsonString = $"\"F{fruitTree.growthStage.Value}:{px},{py}\" 🍎 (果树阻挡)";
                //         else if (debugFeature is Grass) finalJsonString = $"\"G:{px},{py}\" 🌿 (杂草阻挡)";
                //     }
                //     // 5. 家具
                //     else if (location.GetFurnitureAt(currentTile) != null)
                //     {
                //         var f = location.GetFurnitureAt(currentTile);
                //         finalJsonString = f.Name != null && f.Name.Contains("rug") ? $"\"R:{px},{py}\" 🪵 (地毯)" : $"\"O:{px},{py}\" 🪑 (家具阻挡)";
                //     }
                //     // 6. 地图属性判定层
                //     else if (location.doesTileHaveProperty(px, py, "Diggable", "Back") == null || location.doesTileHaveProperty(px, py, "NoSpawn", "Back") != null)
                //     {
                //         finalJsonString = $"\"X:{px},{py}\" 🚧 (不可耕种/不可刷怪区域)";
                //     }

                //     _monitor.Log($"🧭 坐标:({px},{py}) | 📥 JSON内返回的字符串将为 -> {finalJsonString}", LogLevel.Info);
                // }
                // ========================================================================

                string locationName = location.Name ?? "Unknown";
                if (location is FarmHouse) locationName = $"FarmHouse_Level{player.HouseUpgradeLevel}";

                if (locationName != _lastCachedLocation)
                {
                    RefreshWarpCache(location, locationName);
                }

                HashSet<string> obstacles = ScanLocalObstacles(location, player);

                StringBuilder sb = new StringBuilder();
                sb.Append("{\n");
                sb.Append($"  \"location_name\": \"{locationName}\",\n");
                sb.Append($"  \"position\": [{player.StandingPixel.X:F1}, {player.StandingPixel.Y:F1}],\n");
                sb.Append($"  \"tile_coordinate\": [{(int)player.TilePoint.X}, {(int)player.TilePoint.Y}],\n");
                sb.Append($"  \"tile_size\": {Game1.tileSize},\n");
                sb.Append("  \"warps\": [\n    " + string.Join(",\n    ", _cachedWarpJsonList) + "\n  ],\n");
                sb.Append("  \"obstacles\": [" + string.Join(", ", obstacles.Select(s => "\"" + s + "\"")) + "]\n");
                sb.Append("}\nEOF_END\n");

                byte[] data = Encoding.UTF8.GetBytes(sb.ToString());
                lock (_streamLock)
                {
                    if (_netStream != null)
                    {
                        _netStream.Write(data, 0, data.Length);
                        _netStream.Flush();
                    }
                }
            }
            catch
            {
                CleanUp();
            }
        }

        private void RefreshWarpCache(GameLocation location, string locationName)
        {
            _lastCachedLocation = locationName;
            _cachedWarpJsonList.Clear();
            _cachedWarpCoords.Clear();

            var currentMap = location.Map ?? location.map;
            if (currentMap == null || currentMap.Layers == null || currentMap.Layers.Count == 0) return;

            int width = currentMap.Layers[0].LayerWidth;
            int height = currentMap.Layers[0].LayerHeight;

            foreach (var w in location.warps.ToArray())
            {

                if (w == null) continue;
                var warpData = new
                {
                    target_location = w.TargetName,
                    tile_x = w.X,
                    tile_y = w.Y,
                    is_passable = location.isTilePassable(new Vector2(w.X, w.Y))
                };

                _cachedWarpJsonList.Add(JsonConvert.SerializeObject(warpData));
                _cachedWarpCoords.Add($"{w.X},{w.Y}");
            }

            try
            {
                if (location.doors != null)
                {
                    foreach (var door in location.doors.Pairs)
                    {
                        if (!_cachedWarpCoords.Contains($"{door.Key.X},{door.Key.Y}"))
                        {
                            _cachedWarpJsonList.Add($"{{\"target_location\": \"{door.Value}\", \"tile_x\": {door.Key.X}, \"tile_y\": {door.Key.Y}}}");
                            _cachedWarpCoords.Add($"{door.Key.X},{door.Key.Y}");
                        }
                    }
                }
            }
            catch { }

            if (location is Farm farmLocation)
            {
                foreach (Building building in farmLocation.buildings)
                {
                    if (building == null || building.humanDoor.X < 0 || building.humanDoor.Y < 0) continue;
                    int doorX = building.tileX.Value + building.humanDoor.X;
                    int doorY = building.tileY.Value + building.humanDoor.Y;
                    string doorKey = $"{doorX},{doorY}";

                    if (!_cachedWarpCoords.Contains(doorKey))
                    {
                        string buildingType = building.buildingType.Value ?? "";
                        string targetMap = buildingType.Contains("Greenhouse") ? "Greenhouse" :
                                           buildingType.Contains("Cabin") ? "Cabin" :
                                           buildingType.Contains("Farmhouse") ? "FarmHouse" : buildingType;

                        _cachedWarpJsonList.Add($"{{\"target_location\": \"{targetMap}\", \"tile_x\": {doorX}, \"tile_y\": {doorY}}}");
                        _cachedWarpCoords.Add(doorKey);
                    }
                }
            }

            for (int x = 0; x < width; x++)
            {
                for (int y = 0; y < height; y++)
                {
                    if (_cachedWarpCoords.Contains($"{x},{y}")) continue;
                    string combined = (location.doesTileHaveProperty(x, y, "TouchAction", "Back") ?? "") + " " +
                                      (location.doesTileHaveProperty(x, y, "Action", "Buildings") ?? "");
                    if (combined.Contains("Warp"))
                    {
                        string target = "TargetMap";
                        string[] parts = combined.Split(new[] { ' ', '_', ':', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                        foreach (var part in parts)
                        {
                            if (part.Equals("Warp", StringComparison.OrdinalIgnoreCase) || int.TryParse(part, out _)) continue;
                            target = part;
                            break;
                        }
                        _cachedWarpJsonList.Add($"{{\"target_location\": \"{target}\", \"tile_x\": {x}, \"tile_y\": {y}}}");
                        _cachedWarpCoords.Add($"{x},{y}");
                    }
                }
            }
        }

        private HashSet<string> ScanLocalObstacles(GameLocation location, Farmer player)
        {
            HashSet<string> obstacles = new HashSet<string>();
            int scanRange = 30;
            int tx = (int)player.TilePoint.X;
            int ty = (int)player.TilePoint.Y;
            int mapWidth = location.map.Layers[0].LayerWidth;
            int mapHeight = location.map.Layers[0].LayerHeight;

            // 🛠️ 核心剔除点：已完全删去原先一刀切的 doorExemptTiles 收集与跳过逻辑

            // 预扫灌木
            HashSet<string> bushWallTiles = new HashSet<string>();
            var largeFeatures = location.largeTerrainFeatures;
            if (largeFeatures != null)
            {
                foreach (var b in largeFeatures.OfType<Bush>())
                {
                    Rectangle bounds = b.getBoundingBox();
                    for (int bx = bounds.X / 64; bx <= (bounds.X + bounds.Width - 1) / 64; bx++)
                        for (int by = bounds.Y / 64; by <= (bounds.Y + bounds.Height - 1) / 64; by++)
                            bushWallTiles.Add($"{bx},{by}");
                }
            }

            // 预扫大型硬木/陨石
            Dictionary<string, string> clumpTiles = new Dictionary<string, string>();
            if (location.resourceClumps is not null)
            {
                foreach (var clump in location.resourceClumps)
                {
                    if (clump == null) continue;
                    for (int rx = (int)clump.Tile.X; rx < (int)clump.Tile.X + clump.width.Value; rx++)
                        for (int ry = (int)clump.Tile.Y; ry < (int)clump.Tile.Y + clump.height.Value; ry++)
                            clumpTiles[$"{rx},{ry}"] = $"T:{rx},{ry}";
                }
            }

            for (int x = tx - scanRange; x < tx + scanRange; x++)
            {
                for (int y = ty - scanRange; y < ty + scanRange; y++)
                {
                    if (x < 0 || y < 0 || x >= mapWidth || y >= mapHeight)
                    {
                        obstacles.Add($"W:{x},{y}");
                        continue;
                    }

                    string coordKey = $"{x},{y}";
                    Vector2 v = new Vector2(x, y);

                    if (clumpTiles.ContainsKey(coordKey)) { obstacles.Add(clumpTiles[coordKey]); continue; }
                    if (bushWallTiles.Contains(coordKey)) { obstacles.Add($"W:{x},{y}"); continue; }

                    // 1. 建筑高精度碰撞检测（不再受延伸格子的干扰，只通过 CollisionMap 决定墙体）
                    bool blockedByBuilding = false;
                    if (location is Farm farm)
                    {
                        foreach (var building in farm.buildings)
                        {
                            if (building != null && IsTileBlockedByBuilding(building, v))
                            {
                                blockedByBuilding = true;
                                break;
                            }
                        }
                    }

                    if (blockedByBuilding)
                    {
                        obstacles.Add($"W:{x},{y}");
                        continue;
                    }

                    // 2. 原生图层物理通行度判断
                    if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
                    {
                        obstacles.Add($"W:{x},{y}");
                        continue;
                    }

                    // 3. 普通实体对象碰撞
                    if (location.Objects.TryGetValue(v, out StardewValley.Object obj) && obj != null)
                    {
                        if (obj.ParentSheetIndex == 590 || (obj.Name != null && obj.Name.Contains("Artifact Spot")))
                            obstacles.Add($"H:{x},{y}");
                        else if (obj.Name != null && obj.Name.Contains("Stone"))
                            obstacles.Add($"S:{x},{y}");
                        else
                            obstacles.Add($"O:{x},{y}");
                        continue;
                    }

                    // 4. 地表特征（树、草等）
                    if (location.terrainFeatures.TryGetValue(v, out TerrainFeature feature))
                    {
                        if (feature is Tree ordinaryTree) { obstacles.Add($"T{ordinaryTree.growthStage.Value}:{x},{y}"); continue; }
                        if (feature is FruitTree fruitTree) { obstacles.Add($"F{fruitTree.growthStage.Value}:{x},{y}"); continue; }
                        if (feature is Grass) { obstacles.Add($"G:{x},{y}"); continue; }
                    }

                    // 5. 家具检测
                    var furnitureObj = location.GetFurnitureAt(v);
                    if (furnitureObj != null)
                    {
                        obstacles.Add(furnitureObj.Name != null && furnitureObj.Name.Contains("rug") ? $"R:{x},{y}" : $"O:{x},{y}");
                        continue;
                    }

                    // 6. 统一的基础地图无机属性（不可耕种区等）扫描判定
                    if (location.doesTileHaveProperty(x, y, "Diggable", "Back") == null || location.doesTileHaveProperty(x, y, "NoSpawn", "Back") != null)
                    {
                        obstacles.Add($"X:{x},{y}");
                    }
                }
            }
            return obstacles;
        }

        private bool IsTileBlockedByBuilding(Building building, Vector2 tile)
        {
            if (!building.occupiesTile(tile))
                return false;

            int doorX = building.tileX.Value + building.humanDoor.X;
            int doorY = building.tileY.Value + building.humanDoor.Y;
            if ((int)tile.X == doorX && (int)tile.Y == doorY)
                return false;

            string collisionMapStr = building.GetData()?.CollisionMap;

            if (string.IsNullOrEmpty(collisionMapStr))
            {
                if (building.buildingType.Value != null && building.buildingType.Value.Contains("Farmhouse"))
                {
                    return false;
                }
                return true;
            }

            string[] lines = collisionMapStr.Split(
                new[] { '\r', '\n' },
                StringSplitOptions.RemoveEmptyEntries
            ).Select(line => line.Trim()).ToArray();

            if (lines.Length == 0) return true;

            int relativeX = (int)tile.X - building.tileX.Value;
            int relativeY = (int)tile.Y - building.tileY.Value;

            if (relativeY < 0 || relativeY >= lines.Length) return true;
            string targetLine = lines[relativeY];
            if (relativeX < 0 || relativeX >= targetLine.Length) return true;

            char collisionChar = targetLine[relativeX];
            if (collisionChar == 'O' || collisionChar == 'o')
            {
                return false;
            }

            return true;
        }

        public void CleanUp()
        {
            try { _netStream?.Close(); _connectedClient?.Close(); } catch { }
            _netStream = null; _connectedClient = null;
        }
    }
}