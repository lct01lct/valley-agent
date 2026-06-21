using System;
using System.IO;
using System.Text;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Collections.Generic;
using System.Linq;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Objects;
using StardewValley.Locations;
using StardewValley.TerrainFeatures;
using StardewValley.Buildings; // 💡 必须引入建筑命名空间
using Microsoft.Xna.Framework;

namespace StardewMemoryExporter
{
  public class ModEntry : Mod
  {
    private TcpListener _server;
    private TcpClient _connectedClient;
    private NetworkStream _netStream;
    private readonly object _streamLock = new object();
    private static IMonitor _monitor;

    private string _lastCachedLocation = "";
    private List<string> _cachedWarpJsonList = new List<string>();
    private HashSet<string> _cachedWarpCoords = new HashSet<string>();

    public override void Entry(IModHelper helper)
    {
      _monitor = this.Monitor;
      Thread serverThread = new Thread(StartTcpServer) { IsBackground = true };
      serverThread.Start();
      helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
    }

    private void StartTcpServer()
    {
      try
      {
        _server = new TcpListener(IPAddress.Parse("127.0.0.1"), 9999);
        _server.Start();
        while (true)
        {
          TcpClient client = _server.AcceptTcpClient();
          lock (_streamLock) { CleanUp(); _connectedClient = client; _netStream = client.GetStream(); }
        }
      }
      catch (Exception) { }
    }

    private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
    {
      if (_netStream == null || !Context.IsWorldReady || Game1.currentLocation == null) return;

      try
      {
        var location = Game1.currentLocation;
        var player = Game1.player;
        string locationName = location.Name ?? "Unknown";
        if (location is FarmHouse) locationName = $"FarmHouse_Level{player.HouseUpgradeLevel}";

        // ==========================================
        // 🌀 1. 全局传送门扫描 (场景切换缓存)
        // ==========================================
        if (locationName != _lastCachedLocation)
        {
          _lastCachedLocation = locationName;
          _cachedWarpJsonList.Clear();
          _cachedWarpCoords.Clear();

          var currentMap = location.Map ?? location.map;
          if (currentMap != null && currentMap.Layers != null && currentMap.Layers.Count > 0)
          {
            int width = currentMap.Layers[0].LayerWidth;
            int height = currentMap.Layers[0].LayerHeight;

            var warpsList = location.warps;
            if ((object)warpsList != null)
            {
              foreach (var w in warpsList.ToArray())
              {
                if (w == null) continue;
                _cachedWarpJsonList.Add($"{{\"target_location\": \"{w.TargetName}\", \"tile_x\": {w.X}, \"tile_y\": {w.Y}}}");
                _cachedWarpCoords.Add($"{w.X},{w.Y}");
              }
            }

            try
            {
              if (location.doors != null)
              {
                foreach (var door in location.doors.Pairs)
                {
                  int dx = door.Key.X;
                  int dy = door.Key.Y;
                  string targetLocation = door.Value;
                  if (!string.IsNullOrEmpty(targetLocation) && !_cachedWarpCoords.Contains($"{dx},{dy}"))
                  {
                    _cachedWarpJsonList.Add($"{{\"target_location\": \"{targetLocation}\", \"tile_x\": {dx}, \"tile_y\": {dy}}}");
                    _cachedWarpCoords.Add($"{dx},{dy}");
                  }
                }
              }
            }
            catch { }

            // 🌟【基于 1.6 官方源码最终修正 A】：直接提取大门的绝对网格坐标
            if (location is Farm farmLocation)
            {
              foreach (Building building in farmLocation.buildings)
              {
                if (building == null) continue;

                // 💡 1. 抓取建筑左上角绝对坐标
                int startX = building.tileX.Value;
                int startY = building.tileY.Value;

                // 💡 2. 1.6 官方原生字段：直接通过 building.humanDoor 读取大门的相对偏移量
                int doorX = startX + building.humanDoor.X;
                int doorY = startY + building.humanDoor.Y;

                // 防御性拦截：如果门的位置没有初始化（例如地毯、马厩等部分特殊装饰性建筑）则跳过
                if (building.humanDoor.X >= 0 && building.humanDoor.Y >= 0)
                {
                  string doorKey = $"{doorX},{doorY}";
                  if (!_cachedWarpCoords.Contains(doorKey))
                  {
                    // 确定传送门的目标场景名
                    string buildingType = building.buildingType.Value ?? "";
                    string targetMap = "FarmHouse"; // 默认回执

                    if (buildingType.IndexOf("Greenhouse", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                      targetMap = "Greenhouse";
                    }
                    else if (buildingType.IndexOf("Cabin", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                      targetMap = "Cabin";
                    }
                    else if (buildingType.IndexOf("Farmhouse", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                      targetMap = "FarmHouse";
                    }
                    else
                    {
                      targetMap = buildingType; // 比如 "Barn", "Coop" 等动物小屋
                    }

                    _cachedWarpJsonList.Add($"{{\"target_location\": \"{targetMap}\", \"tile_x\": {doorX}, \"tile_y\": {doorY}}}");
                    _cachedWarpCoords.Add(doorKey);
                  }
                }
              }
            }
            for (int x = 0; x < width; x++)
            {
              for (int y = 0; y < height; y++)
              {
                string coordKey = $"{x},{y}";
                if (_cachedWarpCoords.Contains(coordKey)) continue;

                string touchAction = location.doesTileHaveProperty(x, y, "TouchAction", "Back") ??
                                     location.doesTileHaveProperty(x, y, "TouchAction", "Buildings");
                string action = location.doesTileHaveProperty(x, y, "Action", "Buildings") ??
                                location.doesTileHaveProperty(x, y, "Action", "Back");

                string combined = (touchAction ?? "") + " " + (action ?? "");
                if (combined.IndexOf("Warp", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                  string target = "TargetMap";
                  string[] parts = combined.Split(new[] { ' ', '_', ':', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                  foreach (var part in parts)
                  {
                    if (part.Equals("Warp", StringComparison.OrdinalIgnoreCase) ||
                        part.Equals("TouchAction", StringComparison.OrdinalIgnoreCase) ||
                        part.Equals("Action", StringComparison.OrdinalIgnoreCase) || int.TryParse(part, out _)) continue;
                    target = part;
                    break;
                  }
                  _cachedWarpJsonList.Add($"{{\"target_location\": \"{target}\", \"tile_x\": {x}, \"tile_y\": {y}}}");
                  _cachedWarpCoords.Add(coordKey);
                }
              }
            }
          }
        }

        // ==========================================
        // 🧱 2. 分类语义环境扫描 (雷达局部范围)
        // ==========================================
        HashSet<string> obstacles = new HashSet<string>();
        int scanRange = 25;
        int tx = (int)player.TilePoint.X;
        int ty = (int)player.TilePoint.Y;

        int mapWidth = location.map.Layers[0].LayerWidth;
        int mapHeight = location.map.Layers[0].LayerHeight;

        // 🌿【灌木丛 Bush 拓扑重构网格预扫】
        HashSet<string> bushWallTiles = new HashSet<string>();
        if (location.largeTerrainFeatures != null)
        {
          foreach (var largeFeature in location.largeTerrainFeatures)
          {
            if (largeFeature is Bush bush)
            {
              Rectangle bounds = bush.getBoundingBox();
              int minTileX = bounds.X / Game1.tileSize;
              int maxTileX = (bounds.X + bounds.Width - 1) / Game1.tileSize;
              int minTileY = bounds.Y / Game1.tileSize;
              int maxTileY = (bounds.Y + bounds.Height - 1) / Game1.tileSize;
              for (int bx = minTileX; bx <= maxTileX; bx++)
                for (int by = minTileY; by <= maxTileY; by++)
                  bushWallTiles.Add($"{bx},{by}");
            }
          }
        }

        foreach (var pair in location.terrainFeatures.Pairs)
        {
          if (pair.Value is Bush b)
          {
            Rectangle bounds = b.getBoundingBox();
            int minTileX = bounds.X / Game1.tileSize;
            int maxTileX = (bounds.X + bounds.Width - 1) / Game1.tileSize;
            int minTileY = bounds.Y / Game1.tileSize;
            int maxTileY = (bounds.Y + bounds.Height - 1) / Game1.tileSize;
            for (int bx = minTileX; bx <= maxTileX; bx++)
              for (int by = minTileY; by <= maxTileY; by++)
                bushWallTiles.Add($"{bx},{by}");
          }
        }

        // 🌟【新增修复 B】：建筑占地面积实体阻挡网格预扫 (避免isTilePassable漏掉房子身体)
        HashSet<string> buildingOccupiedTiles = new HashSet<string>();
        if (location is Farm currentFarm)
        {
          foreach (Building building in currentFarm.buildings)
          {
            if (building == null) continue;
            int startX = building.tileX.Value;
            int startY = building.tileY.Value;
            int widthBytes = building.tilesWide.Value;
            int heightBytes = building.tilesHigh.Value;

            // 把房子、温室等占用的每一个网格都记录下来
            for (int bx = startX; bx < startX + widthBytes; bx++)
            {
              for (int by = startY; by < startY + heightBytes; by++)
              {
                buildingOccupiedTiles.Add($"{bx},{by}");
              }
            }
          }
        }

        // A. 大型硬木、倒地原木、陨石资源堆探测 (ResourceClumps)
        if (location.resourceClumps != null)
        {
          foreach (var clump in location.resourceClumps)
          {
            if (clump == null) continue;
            int cx = (int)clump.Tile.X; int cy = (int)clump.Tile.Y;
            int cw = clump.width.Value; int ch = clump.height.Value;
            for (int rx = cx; rx < cx + cw; rx++)
            {
              for (int ry = cy; ry < cy + ch; ry++)
              {
                if (Math.Abs(rx - tx) <= scanRange && Math.Abs(ry - ty) <= scanRange)
                  obstacles.Add($"T:{rx},{ry}");
              }
            }
          }
        }

        // 25格网格交叉过滤
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

            // 🌟【触发新增拦截】：如果是温室或大房子的身体网格
            if (buildingOccupiedTiles.Contains(coordKey))
            {
              obstacles.Add($"W:{x},{y}"); // 统一标记为死墙（W:），在小地图上渲染为障碍物
              continue;
            }

            if (bushWallTiles.Contains(coordKey))
            {
              obstacles.Add($"W:{x},{y}");
              continue;
            }

            if (!location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
            {
              obstacles.Add($"W:{x},{y}");
              continue;
            }

            // B. 物品实体检测
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

            // C. 动态植物特征过滤（完美导出树木的 6 个生命周期阶段）
            if (location.terrainFeatures.TryGetValue(v, out TerrainFeature feature))
            {
              // 🌲 普通树（橡树、枫树、松树等）
              if (feature is Tree ordinaryTree)
              {
                int stage = ordinaryTree.growthStage.Value;
                // 发送格式如 "T0:x,y" (种子), "T1:x,y" (小芽), ..., "T5:x,y" (大树)
                obstacles.Add($"T{stage}:{x},{y}");
                continue;
              }
              // 🍎 果树
              else if (feature is FruitTree fruitTree)
              {
                int stage = fruitTree.growthStage.Value;
                // 发送格式如 "F0:x,y" (果树种子), "F1:x,y" (果树幼苗), ..., "F5:x,y" (成熟果树)
                obstacles.Add($"F{stage}:{x},{y}");
                continue;
              }
              // 牧草
              else if (feature is Grass)
              {
                obstacles.Add($"G:{x},{y}");
                continue;
              }
            }

            // D. 家具网格探测
            var furnitureObj = location.GetFurnitureAt(v);
            if (furnitureObj != null)
            {
              if (furnitureObj.Name != null && furnitureObj.Name.IndexOf("rug", StringComparison.OrdinalIgnoreCase) >= 0)
                obstacles.Add($"R:{x},{y}");
              else
                obstacles.Add($"O:{x},{y}");
              continue;
            }

            // E. 种植死地探测
            string diggable = location.doesTileHaveProperty(x, y, "Diggable", "Back");
            string noSpawn = location.doesTileHaveProperty(x, y, "NoSpawn", "Back");
            if (diggable == null || noSpawn != null)
            {
              obstacles.Add($"X:{x},{y}");
            }
          }
        }

        // ==========================================
        // 📜 3. 数据安全打包输出
        // ==========================================
        StringBuilder sb = new StringBuilder();
        sb.Append("{\n");
        sb.Append($"  \"location_name\": \"{locationName}\",\n");
        sb.Append($"  \"position\": [{player.Position.X:F1}, {player.Position.Y:F1}],\n");
        sb.Append($"  \"tile_coordinate\": [{tx}, {ty}],\n");
        sb.Append($"  \"tile_size\": {Game1.tileSize},\n");
        sb.Append("  \"warps\": [\n    " + string.Join(",\n    ", _cachedWarpJsonList) + "\n  ],\n");
        sb.Append("  \"obstacles\": [" + string.Join(", ", obstacles.Select(s => "\"" + s + "\"")) + "]\n");
        sb.Append("}\nEOF_END\n");
        byte[] data = Encoding.UTF8.GetBytes(sb.ToString());
        lock (_streamLock) { if (_netStream != null) { _netStream.Write(data, 0, data.Length); _netStream.Flush(); } }
      }
      catch { CleanUp(); }
    }

    private void CleanUp()
    {
      try { _netStream?.Close(); _connectedClient?.Close(); } catch { }
      _netStream = null; _connectedClient = null;
    }
  }
}