using System;
using System.IO;
using System.Collections.Generic;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Objects;
using StardewValley.Locations;
using Microsoft.Xna.Framework;

namespace StardewMemoryExporter
{
  public class ModEntry : Mod
  {
    private string _exportPath;

    public override void Entry(IModHelper helper)
    {
      _exportPath = Path.Combine(helper.DirectoryPath, "game_state.json");
      helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
    }

    private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
    {
      if (!Context.IsWorldReady || Game1.currentLocation == null) return;

      // 1. 提取玩家当前的绝对内存像素位置 [X, Y]
      float playerX = Game1.player.Position.X;
      float playerY = Game1.player.Position.Y;

      // 2. 提取当前具体场景的名称
      var location = Game1.currentLocation;
      string sceneName = location.Name ?? "UnknownScene";

      // 🧱 【终极修复位】：规避命名空间内部字段变更，直接从 player 层面拿最稳定的升级级数
      if (location is FarmHouse)
      {
        sceneName = $"FarmHouse_Level{Game1.player.HouseUpgradeLevel}";
      }

      int width = location.map.Layers[0].LayerWidth;
      int height = location.map.Layers[0].LayerHeight;

      HashSet<string> obstacleSet = new HashSet<string>();

      // ----------------------------------------------------
      // 策略 A：扫描地图固有的硬性静态碰撞（墙壁、边界）
      // ----------------------------------------------------
      for (int x = 0; x < width; x++)
      {
        for (int y = 0; y < height; y++)
        {
          var tileLocation = new xTile.Dimensions.Location(x, y);
          if (!location.isTilePassable(tileLocation, Game1.viewport))
          {
            obstacleSet.Add($"{x},{y}");
          }
        }
      }

      // ----------------------------------------------------
      // 策略 B：提取【床、电视机、沙发、桌子等所有家具】占用的地块格子
      // ----------------------------------------------------
      foreach (var furniture in location.furniture)
      {
        int startX = (int)furniture.TileLocation.X;
        int startY = (int)furniture.TileLocation.Y;

        var boundingBox = furniture.GetBoundingBox();

        int tileWidth = boundingBox.Width / 64;
        int tileHeight = boundingBox.Height / 64;

        for (int x = startX; x < startX + Math.Max(1, tileWidth); x++)
        {
          for (int y = startY; y < startY + Math.Max(1, tileHeight); y++)
          {
            if (x >= 0 && x < width && y >= 0 && y < height)
            {
              obstacleSet.Add($"{x},{y}");
            }
          }
        }
      }

      // ----------------------------------------------------
      // 策略 C：提取【储物箱、手工艺品、围栏等放置物】占用的地块格子
      // ----------------------------------------------------
      foreach (var pair in location.objects.Pairs)
      {
        Vector2 tilePos = pair.Key;
        StardewValley.Object obj = pair.Value;

        if (!obj.isPassable())
        {
          obstacleSet.Add($"{(int)tilePos.X},{(int)tilePos.Y}");
        }
      }

      // 3. 将所有扫描到的障碍物坐标转换为 JSON 格式列表
      List<string> formattedObstacles = new List<string>();
      foreach (var obs in obstacleSet)
      {
        formattedObstacles.Add($"\"{obs}\"");
      }

      // 4. 组装标准 JSON 文本
      string json = "{\n" +
                    $"  \"scene_name\": \"{sceneName}\",\n" +
                    $"  \"player_px\": [{playerX:F1}, {playerY:F1}],\n" +
                    $"  \"tile_size\": 64,\n" +
                    "  \"obstacles\": [" + string.Join(", ", formattedObstacles) + "]\n" +
                    "}";

      try
      {
        File.WriteAllText(_exportPath, json);
      }
      catch (IOException) { }
    }
  }
}