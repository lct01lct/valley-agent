using System;
using System.IO;
using System.Reflection;
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
using StardewValley.Tools;
using Microsoft.Xna.Framework;
using Newtonsoft.Json;
using StardewModdingAPI.Events;
using StardewValley.Menus;

namespace StardewMemoryExporter
{
    public class StardewObserver
    {
        private TcpListener _server;
        private TcpClient _connectedClient;
        private NetworkStream _netStream;
        private readonly object _streamLock = new object();
        private readonly IMonitor _monitor;
        private volatile bool _isStopping = false;

        private string _lastCachedLocation = "";
        private readonly List<object> _cachedWarpDataList = new List<object>();
        private readonly HashSet<string> _cachedWarpCoords = new HashSet<string>();
        private const int ObstacleScanRange = 25;
        private const int HeavyStateIntervalMs = 150;
        private long _lastHeavyStateAtMs = 0;
        private string _lastHeavyStateLocation = "";
        private Point _lastHeavyStatePlayerTile = new Point(int.MinValue, int.MinValue);
        private List<string> _cachedObstacleList = new List<string>();
        private List<object> _cachedFarmTiles = new List<object>();

        private List<string> _lastHudMessages = new List<string>();
        // 🛡️ 调试专用变量：记录上一次打印的玩家格子坐标，防止高频刷屏
        private Vector2 _lastLoggedPlayerTile = Vector2.Zero;

        private readonly SharedBlackboard _blackboard;

        public StardewObserver(IMonitor monitor, SharedBlackboard blackboard, int port = 9999)
        {
            _monitor = monitor;
            _blackboard = blackboard;
            Thread serverThread = new Thread(() => StartTcpServer(port)) { IsBackground = true };
            serverThread.Start();
        }

        private void StartTcpServer(int port)
        {
            try
            {
                _server = new TcpListener(IPAddress.Parse("127.0.0.1"), port);
                _server.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                _server.Start();
                while (!_isStopping)
                {
                    try
                    {
                        TcpClient client = _server.AcceptTcpClient();
                        lock (_streamLock)
                        {
                            CloseClientConnection();
                            _connectedClient = client;
                            _netStream = client.GetStream();
                        }
                        _monitor.Log("📡 [StardewObserver] Python 大脑已成功连入场景雷达数据流！", LogLevel.Info);
                    }
                    catch (SocketException ex)
                    {
                        if (_isStopping)
                        {
                            _monitor.Log("🛑 [StardewObserver] 雷达服务器已停止监听。", LogLevel.Trace);
                            break;
                        }

                        _monitor.Log($"❌ [StardewObserver] 雷达服务器 Socket 异常: {ex.Message}", LogLevel.Error);
                    }
                    catch (ObjectDisposedException)
                    {
                        if (_isStopping)
                        {
                            _monitor.Log("🛑 [StardewObserver] 雷达服务器资源已释放。", LogLevel.Trace);
                            break;
                        }

                        _monitor.Log("❌ [StardewObserver] 雷达服务器监听器被意外释放。", LogLevel.Error);
                    }
                }
            }
            catch (Exception ex)
            {
                if (_isStopping)
                {
                    _monitor.Log("🛑 [StardewObserver] 雷达服务器已随游戏退出。", LogLevel.Trace);
                    return;
                }

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
                // if (location is FarmHouse) locationName = $"FarmHouse_Level{player.HouseUpgradeLevel}";

                if (locationName != _lastCachedLocation)
                {
                    RefreshWarpCache(location, locationName);
                }

                var currentMap = location.Map ?? location.map;
                int mapWidth = currentMap?.Layers?[0]?.LayerWidth ?? 0;
                int mapHeight = currentMap?.Layers?[0]?.LayerHeight ?? 0;
                Point currentPlayerTile = new Point((int)player.TilePoint.X, (int)player.TilePoint.Y);
                long nowMs = Environment.TickCount64;
                bool shouldRefreshHeavyState =
                    locationName != _lastHeavyStateLocation
                    || currentPlayerTile != _lastHeavyStatePlayerTile
                    || nowMs - _lastHeavyStateAtMs >= HeavyStateIntervalMs;

                if (shouldRefreshHeavyState)
                {
                    _cachedObstacleList = ScanLocalObstacles(location, player).ToList();
                    _cachedFarmTiles = CreateFarmTilesSnapshot(location, player);
                    _lastHeavyStateAtMs = nowMs;
                    _lastHeavyStateLocation = locationName;
                    _lastHeavyStatePlayerTile = currentPlayerTile;
                }

                // if (locationName == "Farm")
                // {
                //     var debugTiles = new[] { (63, 29), (61, 30) };
                //     foreach (var (tileX, tileY) in debugTiles)
                //     {
                //         string obstacleSummary = GetObstacleTypeAtTile(location, player, tileX, tileY);
                //         _monitor.Log($"🧭 [FarmDebug] tile({tileX},{tileY}) => {obstacleSummary}", LogLevel.Info);
                //     }
                // }

                var stateSnapshot = new
                {
                    location_name = locationName,
                    position = new[]
                    {
                        Math.Round((double)player.StandingPixel.X, 1),
                        Math.Round((double)player.StandingPixel.Y, 1),
                    },
                    tile_coordinate = new[] { (int)player.TilePoint.X, (int)player.TilePoint.Y },
                    tile_size = Game1.tileSize,
                    state_scope = "local",
                    scan_range = ObstacleScanRange,
                    map_size = new[] { mapWidth, mapHeight },
                    CurrentToolIndex = player.CurrentToolIndex,
                    CurrentToolbarIndex = player.CurrentToolIndex / 12,
                    UsingTool = player.UsingTool,
                    CanMove = player.CanMove,
                    IsPlayerFree = Context.IsPlayerFree,
                    CanPlayerMove = Context.CanPlayerMove,
                    Items = CreateItemsSnapshot(player),
                    ToolTarget = CreateToolTargetSnapshot(player),
                    // 轻量帧不重复发送重扫描数据；Python 端会沿用上一份 obstacles/FarmTiles。
                    FarmTiles = shouldRefreshHeavyState ? _cachedFarmTiles : null,
                    warps = _cachedWarpDataList,
                    obstacles = shouldRefreshHeavyState ? _cachedObstacleList : null,
                };

                string packet = JsonConvert.SerializeObject(stateSnapshot) + "\nEOF_END\n";
                byte[] data = Encoding.UTF8.GetBytes(packet);
                lock (_streamLock)
                {
                    if (_netStream != null)
                    {
                        _netStream.Write(data, 0, data.Length);
                        _netStream.Flush();
                    }
                }
            }
            catch (Exception ex)
            {
                _monitor.Log(
                    $"🔌 [StardewObserver] Python 状态客户端断开或写入失败，保留 Observer 监听等待重连: {ex.Message}",
                    LogLevel.Trace
                );
                CloseClientConnection();
            }
        }

        private void RefreshWarpCache(GameLocation location, string locationName)
        {
            _lastCachedLocation = locationName;
            _cachedWarpDataList.Clear();
            _cachedWarpCoords.Clear();

            var currentMap = location.Map ?? location.map;
            if (currentMap == null || currentMap.Layers == null || currentMap.Layers.Count == 0) return;

            int width = currentMap.Layers[0].LayerWidth;
            int height = currentMap.Layers[0].LayerHeight;

            foreach (var w in location.warps.ToArray())
            {

                if (w == null) continue;
                _cachedWarpDataList.Add(CreateWarpSnapshot(
                    w.TargetName,
                    w.X,
                    w.Y,
                    location.isTilePassable(new Vector2(w.X, w.Y))
                ));
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
                            _cachedWarpDataList.Add(CreateWarpSnapshot(door.Value, door.Key.X, door.Key.Y));
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

                        _cachedWarpDataList.Add(CreateWarpSnapshot(targetMap, doorX, doorY));
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
                        _cachedWarpDataList.Add(CreateWarpSnapshot(target, x, y));
                        _cachedWarpCoords.Add($"{x},{y}");
                    }
                }
            }
        }

        private object CreateWarpSnapshot(string targetLocation, int tileX, int tileY, bool? isPassable = null)
        {
            if (isPassable.HasValue)
            {
                return new
                {
                    target_location = targetLocation,
                    tile_x = tileX,
                    tile_y = tileY,
                    is_passable = isPassable.Value,
                };
            }

            return new
            {
                target_location = targetLocation,
                tile_x = tileX,
                tile_y = tileY,
            };
        }

        private List<object> CreateItemsSnapshot(Farmer player)
        {
            var items = new List<object>();

            for (int index = 0; index < player.Items.Count; index++)
            {
                Item item = player.Items[index];
                if (item == null) continue;

                items.Add(new
                {
                    Index = index,
                    Name = item.Name ?? "",
                    DisplayName = item.DisplayName ?? "",
                    QualifiedItemId = item.QualifiedItemId ?? "",
                    Category = item.Category,
                    Stack = item.Stack,
                    IsTool = item is Tool,
                    WaterLeft = item is WateringCan wateringCan ? ReadOptionalIntMember(wateringCan, "WaterLeft") : null,
                    WaterCapacity = item is WateringCan wateringCanCapacity ? ReadOptionalIntMember(wateringCanCapacity, "waterCanMax") : null,
                });
            }

            return items;
        }

        private int? ReadOptionalIntMember(object source, string memberName)
        {
            if (source == null) return null;

            BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            Type sourceType = source.GetType();
            PropertyInfo propertyInfo = sourceType.GetProperty(memberName, flags);
            if (propertyInfo != null)
            {
                return ConvertOptionalIntValue(propertyInfo.GetValue(source));
            }

            FieldInfo fieldInfo = sourceType.GetField(memberName, flags);
            if (fieldInfo != null)
            {
                return ConvertOptionalIntValue(fieldInfo.GetValue(source));
            }

            return null;
        }

        private int? ConvertOptionalIntValue(object value)
        {
            if (value == null) return null;
            if (value is int intValue) return intValue;

            PropertyInfo valueProperty = value.GetType().GetProperty("Value");
            if (valueProperty != null)
            {
                object innerValue = valueProperty.GetValue(value);
                if (innerValue is int innerIntValue) return innerIntValue;
            }

            if (value is IConvertible convertible)
            {
                try
                {
                    return convertible.ToInt32(null);
                }
                catch
                {
                    return null;
                }
            }

            return null;
        }

        private object CreateToolTargetSnapshot(Farmer player)
        {
            Point playerTile = player.TilePoint;
            int facingDirection = player.FacingDirection;
            Point targetTile = GetFacingTargetTile(playerTile, facingDirection);

            return new
            {
                Source = "FacingDirection",
                Tile = new[] { targetTile.X, targetTile.Y },
                PlayerTile = new[] { playerTile.X, playerTile.Y },
                FacingDirection = facingDirection,
                SelectedItemName = player.CurrentItem?.Name ?? "",
                IsStandingOnTarget = targetTile.X == playerTile.X && targetTile.Y == playerTile.Y,
                IsCardinalNeighbor = Math.Abs(targetTile.X - playerTile.X) + Math.Abs(targetTile.Y - playerTile.Y) == 1,
            };
        }

        private Point GetFacingTargetTile(Point playerTile, int facingDirection)
        {
            return facingDirection switch
            {
                0 => new Point(playerTile.X, playerTile.Y - 1),
                1 => new Point(playerTile.X + 1, playerTile.Y),
                2 => new Point(playerTile.X, playerTile.Y + 1),
                3 => new Point(playerTile.X - 1, playerTile.Y),
                _ => playerTile,
            };
        }

        private List<object> CreateFarmTilesSnapshot(GameLocation location, Farmer player)
        {
            var farmTiles = new List<object>();
            int playerTileX = (int)player.TilePoint.X;
            int playerTileY = (int)player.TilePoint.Y;
            int mapWidth = location.map.Layers[0].LayerWidth;
            int mapHeight = location.map.Layers[0].LayerHeight;

            int startX = Math.Max(0, playerTileX - ObstacleScanRange);
            int endX = Math.Min(mapWidth, playerTileX + ObstacleScanRange);
            int startY = Math.Max(0, playerTileY - ObstacleScanRange);
            int endY = Math.Min(mapHeight, playerTileY + ObstacleScanRange);

            for (int x = startX; x < endX; x++)
            {
                for (int y = startY; y < endY; y++)
                {
                    farmTiles.Add(CreateFarmTileSnapshot(location, x, y));
                }
            }

            return farmTiles;
        }

        private object CreateFarmTileSnapshot(GameLocation location, int x, int y)
        {
            Vector2 tile = new Vector2(x, y);
            string terrainFeatureType = "";
            int state = 0;
            bool isWatered = false;
            bool hasCrop = false;
            object cropSnapshot = null;

            if (location.terrainFeatures.TryGetValue(tile, out TerrainFeature feature))
            {
                terrainFeatureType = feature.GetType().Name;

                if (feature is HoeDirt hoeDirt)
                {
                    var crop = hoeDirt.crop;
                    hasCrop = crop != null;
                    state = hoeDirt.state.Value;
                    isWatered = hoeDirt.state.Value == 1;
                    cropSnapshot = hasCrop ? new
                    {
                        NetSeedIndex = crop.netSeedIndex.Value,
                        IndexOfHarvest = crop.indexOfHarvest.Value,
                        CurrentPhase = crop.currentPhase.Value,
                        Dead = crop.dead.Value,
                        ForageCrop = crop.forageCrop.Value,
                    } : null;
                }
            }

            bool hasHoeDirt = terrainFeatureType == "HoeDirt";
            string obstacleType = GetFarmTileObstacleType(location, tile);
            bool isDiggable = location.doesTileHaveProperty(x, y, "Diggable", "Back") != null;
            bool hasNoSpawn = location.doesTileHaveProperty(x, y, "NoSpawn", "Back") != null;
            bool isPassable = location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport);
            bool hasBlockingObstacle = !string.IsNullOrEmpty(obstacleType);

            return new
            {
                Tile = new[] { x, y },
                TerrainFeatureType = terrainFeatureType,
                State = state,
                // HoeDirt.state == 1 表示当前耕地已浇水；这里导出派生字段，方便 Python 端直接验证动作结果。
                IsWatered = isWatered,
                HasCrop = hasCrop,
                Crop = cropSnapshot,
                // 以下是 Python FarmNode 做 P1 批处理规划所需的派生能力字段。
                // CanHoe 表示“当前这一帧无需先清障即可直接锄地”；清障后会在新快照中重新计算。
                CanHoe = isDiggable && !hasNoSpawn && !hasHoeDirt && isPassable && !hasBlockingObstacle,
                CanPlant = hasHoeDirt && !hasCrop,
                HasHoeDirt = hasHoeDirt,
                ObstacleType = obstacleType,
                IsDiggable = isDiggable,
                HasNoSpawn = hasNoSpawn,
                IsPassable = isPassable,
            };
        }

        private string GetFarmTileObstacleType(GameLocation location, Vector2 tile)
        {
            int x = (int)tile.X;
            int y = (int)tile.Y;

            if (location is Farm farm)
            {
                foreach (var building in farm.buildings)
                {
                    if (building != null && IsTileBlockedByBuilding(building, tile))
                    {
                        return "Wall";
                    }
                }
            }

            if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
            {
                return "Wall";
            }

            if (location.Objects.TryGetValue(tile, out StardewValley.Object obj) && obj != null)
            {
                if (obj.ParentSheetIndex == 590 || (obj.Name != null && obj.Name.Contains("Artifact Spot")))
                    return "Worm";
                if (obj.Name != null && obj.Name.Contains("Stone"))
                    return "Stone";
                if (obj.Name != null && obj.Name.Contains("Weeds"))
                    return "Weeds";
                if (obj.Name != null && obj.Name.Contains("Twig"))
                    return "Twig";
                return "Object";
            }

            if (location.terrainFeatures.TryGetValue(tile, out TerrainFeature feature))
            {
                if (feature is HoeDirt)
                    return "";
                if (feature is Tree ordinaryTree)
                    return $"Tree{Math.Min(ordinaryTree.growthStage.Value, 5)}";
                if (feature is FruitTree fruitTree)
                    return $"FruitTree{Math.Min(fruitTree.growthStage.Value, 5)}";
                if (feature is Grass)
                    return "Grass";
                return "Object";
            }

            var furnitureObj = location.GetFurnitureAt(tile);
            if (furnitureObj != null)
            {
                return furnitureObj.Name != null && furnitureObj.Name.Contains("rug") ? "Rug" : "Object";
            }

            return "";
        }

        // private string GetObstacleTypeAtTile(GameLocation location, Farmer player, int x, int y)
        // {
        //     if (x < 0 || y < 0 || x >= location.map.Layers[0].LayerWidth || y >= location.map.Layers[0].LayerHeight)
        //     {
        //         return "W: out_of_bounds";
        //     }

        //     Vector2 v = new Vector2(x, y);

        //     if (location is Farm farm)
        //     {
        //         foreach (var building in farm.buildings)
        //         {
        //             if (building != null && IsTileBlockedByBuilding(building, v))
        //             {
        //                 return "W: building";
        //             }
        //         }
        //     }

        //     if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
        //     {
        //         return "W: map_collision";
        //     }

        //     if (location.Objects.TryGetValue(v, out StardewValley.Object obj) && obj != null)
        //     {
        //         if (obj.ParentSheetIndex == 590 || (obj.Name != null && obj.Name.Contains("Artifact Spot")))
        //             return "H: artifact_spot";
        //         else if (obj.Name != null && obj.Name.Contains("Stone"))
        //             return "S: stone";
        //         else
        //             return "O: object";
        //     }

        //     if (location.terrainFeatures.TryGetValue(v, out TerrainFeature feature))
        //     {
        //         if (feature is Tree ordinaryTree) return $"T{ordinaryTree.growthStage.Value}: tree";
        //         if (feature is FruitTree fruitTree) return $"F{fruitTree.growthStage.Value}: fruit_tree";
        //         if (feature is Grass) return "G: grass";
        //     }

        //     var furnitureObj = location.GetFurnitureAt(v);
        //     if (furnitureObj != null)
        //     {
        //         return furnitureObj.Name != null && furnitureObj.Name.Contains("rug") ? "R: rug" : "O: furniture";
        //     }

        //     if (location.doesTileHaveProperty(x, y, "Diggable", "Back") == null || location.doesTileHaveProperty(x, y, "NoSpawn", "Back") != null)
        //     {
        //         return "X: non_diggable_or_no_spawn";
        //     }

        //     return "None";
        // }

        private HashSet<string> ScanLocalObstacles(GameLocation location, Farmer player)
        {
            return ScanObstacles(location, player, ObstacleScanRange);
        }

        private HashSet<string> ScanGlobalObstacles(GameLocation location, Farmer player)
        {
            return ScanObstacles(location, player, null);
        }

        private HashSet<string> ScanObstacles(GameLocation location, Farmer player, int? scanRange)
        {
            HashSet<string> obstacles = new HashSet<string>();
            int tx = (int)player.TilePoint.X;
            int ty = (int)player.TilePoint.Y;
            int mapWidth = location.map.Layers[0].LayerWidth;
            int mapHeight = location.map.Layers[0].LayerHeight;



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
                            clumpTiles[$"{rx},{ry}"] = $"TreeStump:{rx},{ry}";
                }
            }

            int startX = scanRange.HasValue ? tx - scanRange.Value : 0;
            int endX = scanRange.HasValue ? tx + scanRange.Value : mapWidth;
            int startY = scanRange.HasValue ? ty - scanRange.Value : 0;
            int endY = scanRange.HasValue ? ty + scanRange.Value : mapHeight;

            for (int x = startX; x < endX; x++)
            {
                for (int y = startY; y < endY; y++)
                {
                    if (x < 0 || y < 0 || x >= mapWidth || y >= mapHeight)
                    {
                        obstacles.Add($"Wall:{x},{y}");
                        continue;
                    }

                    string coordKey = $"{x},{y}";
                    Vector2 v = new Vector2(x, y);




                    if (clumpTiles.ContainsKey(coordKey)) { obstacles.Add(clumpTiles[coordKey]); continue; }
                    if (bushWallTiles.Contains(coordKey)) { obstacles.Add($"Wall:{x},{y}"); continue; }

                    // if (location.Objects.TryGetValue(v, out StardewValley.Object obje) && obje != null)
                    // {
                    //     _monitor.Log($"📍 格子 ({x}, {y}) 上的物品:{obje.DisplayName}({obje.Name}) (ID: {obje.ParentSheetIndex})", LogLevel.Info);
                    // }

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
                        obstacles.Add($"Wall:{x},{y}");
                        continue;
                    }

                    // 2. 原生图层物理通行度判断
                    if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
                    {
                        obstacles.Add($"Wall:{x},{y}");
                        continue;
                    }

                    // 3. 普通实体对象碰撞
                    if (location.Objects.TryGetValue(v, out StardewValley.Object obj) && obj != null)
                    {


                        if (obj.ParentSheetIndex == 590 || (obj.Name != null && obj.Name.Contains("Artifact Spot")))
                            obstacles.Add($"Worm:{x},{y}");
                        else if (obj.Name != null && obj.Name.Contains("Stone"))
                            obstacles.Add($"Stone:{x},{y}");
                        else if (obj.Name != null && obj.Name.Contains("Weeds"))
                            obstacles.Add($"Weeds:{x},{y}");
                        else if (obj.Name != null && obj.Name.Contains("Twig"))
                            obstacles.Add($"Twig:{x},{y}");
                        else
                            obstacles.Add($"Object:{x},{y}");
                        continue;
                    }

                    // 4. 地表特征（树、草等）
                    if (location.terrainFeatures.TryGetValue(v, out TerrainFeature feature))
                    {
                        if (feature is Tree ordinaryTree) { obstacles.Add($"Tree{Math.Min(ordinaryTree.growthStage.Value, 5)}:{x},{y}"); continue; }
                        if (feature is FruitTree fruitTree) { obstacles.Add($"FruitTree{Math.Min(fruitTree.growthStage.Value, 5)}:{x},{y}"); continue; }
                        if (feature is Grass) { obstacles.Add($"Grass:{x},{y}"); continue; }
                    }

                    // 5. 家具检测
                    var furnitureObj = location.GetFurnitureAt(v);
                    if (furnitureObj != null)
                    {
                        obstacles.Add(furnitureObj.Name != null ? $"Furniture|{furnitureObj.Name}:{x},{y}" : $"Object:{x},{y}");
                        continue;
                    }

                    // 6. 统一的基础地图无机属性（不可耕种区等）扫描判定
                    if (location.doesTileHaveProperty(x, y, "Diggable", "Back") == null || location.doesTileHaveProperty(x, y, "NoSpawn", "Back") != null)
                    {
                        obstacles.Add($"Dead:{x},{y}");
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
            _isStopping = true;
            CloseClientConnection();

            try
            {
                _server?.Stop();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [StardewObserver] 停止雷达监听器时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            _server = null;
        }

        private void CloseClientConnection()
        {
            try
            {
                _netStream?.Close();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [StardewObserver] 关闭雷达数据流时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            try
            {
                _connectedClient?.Close();
            }
            catch (Exception ex)
            {
                _monitor.Log($"⚠️ [StardewObserver] 关闭雷达客户端时发生异常，已忽略: {ex.Message}", LogLevel.Trace);
            }

            _netStream = null;
            _connectedClient = null;
        }
    }
}
