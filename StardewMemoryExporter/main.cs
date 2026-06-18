using System;
using System.IO;
using System.Text;
using System.Net;
using System.Net.Sockets;
using System.Threading;
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
    private TcpListener _server;
    private TcpClient _connectedClient;
    private NetworkStream _netStream;
    private readonly object _streamLock = new object();
    private bool _isRunning = true;

    public override void Entry(IModHelper helper)
    {
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
        while (_isRunning)
        {
          TcpClient client = _server.AcceptTcpClient();
          lock (_streamLock)
          {
            CleanUp();
            _connectedClient = client;
            _netStream = client.GetStream();
          }
        }
      }
      catch (Exception) { }
    }

    private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
    {
      if (_netStream == null || !Context.IsWorldReady || Game1.currentLocation == null) return;

      try
      {
        float playerX = Game1.player.Position.X;
        float playerY = Game1.player.Position.Y;
        var location = Game1.currentLocation;
        string sceneName = location.Name ?? "UnknownScene";

        if (location is FarmHouse)
        {
          sceneName = $"FarmHouse_Level{Game1.player.HouseUpgradeLevel}";
        }

        int width = location.map.Layers[0].LayerWidth;
        int height = location.map.Layers[0].LayerHeight;

        HashSet<string> obstacleSet = new HashSet<string>();

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
              if (x >= 0 && x < width && y >= 0 && y < height) obstacleSet.Add($"{x},{y}");
            }
          }
        }

        foreach (var pair in location.objects.Pairs)
        {
          if (!pair.Value.isPassable()) obstacleSet.Add($"{(int)pair.Key.X},{(int)pair.Key.Y}");
        }

        List<string> formattedObstacles = new List<string>();
        foreach (var obs in obstacleSet) formattedObstacles.Add($"\"{obs}\"");

        // 🚨【终极修复：使用 StringBuilder 保证大包绝对闭合】
        StringBuilder sb = new StringBuilder();
        sb.Append("{\n");
        sb.Append($"  \"scene_name\": \"{sceneName}\",\n");
        sb.Append($"  \"player_px\": [{playerX:F1}, {playerY:F1}],\n");
        sb.Append($"  \"tile_size\": 64,\n");
        sb.Append("  \"obstacles\": [" + string.Join(", ", formattedObstacles) + "]\n");
        sb.Append("}\nEOF_END\n"); // 追加明确的包结束符

        byte[] data = Encoding.UTF8.GetBytes(sb.ToString());
        lock (_streamLock)
        {
          if (_netStream != null && _connectedClient.Connected)
          {
            _netStream.Write(data, 0, data.Length);
            _netStream.Flush();
          }
        }
      }
      catch (Exception) { CleanUp(); }
    }

    private void CleanUp()
    {
      try { _netStream?.Close(); } catch { }
      try { _connectedClient?.Close(); } catch { }
      _netStream = null;
      _connectedClient = null;
    }
  }
}