using System;
using System.Collections.Generic;
using Microsoft.Xna.Framework;
using Newtonsoft.Json.Linq;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Objects;

namespace StardewMemoryExporter
{
    internal static class MineStateScanner
    {
        public static List<object> CreateLaddersSnapshot(GameLocation location)
        {
            var ladders = new List<object>();
            if (location is not MineShaft)
            {
                return ladders;
            }

            HashSet<string> seenTiles = new HashSet<string>();
            foreach (var pair in location.Objects.Pairs)
            {
                StardewValley.Object obj = pair.Value;
                if (obj == null || !IsLadderObject(obj))
                {
                    continue;
                }

                seenTiles.Add(BuildTileKey((int)pair.Key.X, (int)pair.Key.Y));
                ladders.Add(CreateLadderObjectSnapshot(pair.Key, obj));
            }

            foreach (object ladderTile in CreateLadderBuildingTileSnapshots(location, seenTiles))
            {
                ladders.Add(ladderTile);
            }

            foreach (object ladderAction in CreateLadderActionTileSnapshots(location, seenTiles))
            {
                ladders.Add(ladderAction);
            }

            return ladders;
        }

        public static List<object> CreateMiningNodesSnapshot(GameLocation location)
        {
            var miningNodes = new List<object>();
            if (location is not MineShaft)
            {
                return miningNodes;
            }

            foreach (var pair in location.Objects.Pairs)
            {
                StardewValley.Object obj = pair.Value;
                if (obj == null || !IsMiningNodeObject(obj) && !IsMineShaftBreakableObject(obj))
                {
                    continue;
                }

                miningNodes.Add(new
                {
                    Tile = new[] { (int)pair.Key.X, (int)pair.Key.Y },
                    Type = GetMiningNodeType(obj),
                    Name = obj.Name ?? "",
                    DisplayName = obj.DisplayName ?? "",
                    QualifiedItemId = obj.QualifiedItemId ?? "",
                    ParentSheetIndex = obj.ParentSheetIndex,
                });
            }

            return miningNodes;
        }

        public static List<object> CreateMineEntrancesSnapshot(GameLocation location)
        {
            var entrances = new List<object>();
            if (!string.Equals(location.Name, "Mine", StringComparison.OrdinalIgnoreCase))
            {
                return entrances;
            }

            var currentMap = location.Map ?? location.map;
            if (currentMap == null || currentMap.Layers == null || currentMap.Layers.Count == 0)
            {
                return entrances;
            }

            int width = currentMap.Layers[0].LayerWidth;
            int height = currentMap.Layers[0].LayerHeight;

            for (int x = 0; x < width; x++)
            {
                for (int y = 0; y < height; y++)
                {
                    string action = location.doesTileHaveProperty(x, y, "Action", "Buildings") ?? "";
                    string touchAction = location.doesTileHaveProperty(x, y, "TouchAction", "Back") ?? "";
                    string combined = $"{action} {touchAction}";
                    if (!LooksLikeMineEntranceAction(combined))
                    {
                        continue;
                    }

                    entrances.Add(new
                    {
                        Tile = new[] { x, y },
                        Type = "MineEntrance",
                        Source = string.IsNullOrWhiteSpace(action) ? "TouchAction" : "Action",
                        Action = combined.Trim(),
                    });
                }
            }

            return entrances;
        }

        public static JObject QueryLadderAtTile(GameLocation location, Vector2 tile)
        {
            int tileX = (int)tile.X;
            int tileY = (int)tile.Y;
            if (location is not MineShaft)
            {
                return CreateQueryResponse("SUCCESS", "NOT_MINE_SHAFT", false, tileX, tileY);
            }

            if (location.Objects.TryGetValue(tile, out StardewValley.Object obj) && obj != null && IsLadderObject(obj))
            {
                return CreateQueryResponse("SUCCESS", "", true, tileX, tileY, JObject.FromObject(CreateLadderObjectSnapshot(tile, obj)));
            }

            int? buildingTileIndex = ReadBuildingTileIndex(location, tileX, tileY);
            if (buildingTileIndex == 173)
            {
                return CreateQueryResponse(
                    "SUCCESS",
                    "",
                    true,
                    tileX,
                    tileY,
                    JObject.FromObject(CreateLadderBuildingTileSnapshot(tileX, tileY))
                );
            }

            string actionText = ReadCombinedTileAction(location, tileX, tileY);
            if (LooksLikeNextLevelLadderAction(actionText))
            {
                return CreateQueryResponse(
                    "SUCCESS",
                    "",
                    true,
                    tileX,
                    tileY,
                    JObject.FromObject(CreateLadderActionTileSnapshot(tileX, tileY, actionText))
                );
            }

            return CreateQueryResponse("SUCCESS", "LADDER_NOT_FOUND", false, tileX, tileY);
        }

        private static JObject CreateQueryResponse(
            string status,
            string reason,
            bool hasLadder,
            int tileX,
            int tileY,
            JObject ladder = null
        )
        {
            var response = new JObject
            {
                ["status"] = status,
                ["reason"] = reason,
                ["has_ladder"] = hasLadder,
                ["tile"] = new JArray(tileX, tileY),
            };

            if (ladder != null)
            {
                response["ladder"] = ladder;
            }

            return response;
        }

        private static object CreateLadderObjectSnapshot(Vector2 tile, StardewValley.Object obj)
        {
            return new
            {
                Tile = new[] { (int)tile.X, (int)tile.Y },
                Type = "Ladder",
                Name = obj.Name ?? "",
                DisplayName = obj.DisplayName ?? "",
                QualifiedItemId = obj.QualifiedItemId ?? "",
                ParentSheetIndex = obj.ParentSheetIndex,
                Source = "Object",
                Action = "",
            };
        }

        private static object CreateLadderBuildingTileSnapshot(int tileX, int tileY)
        {
            return new
            {
                Tile = new[] { tileX, tileY },
                Type = "Ladder",
                Name = "",
                DisplayName = "",
                QualifiedItemId = "",
                ParentSheetIndex = 173,
                Source = "BuildingsTileIndex",
                Action = "TileIndex=173",
            };
        }

        private static object CreateLadderActionTileSnapshot(int tileX, int tileY, string action)
        {
            return new
            {
                Tile = new[] { tileX, tileY },
                Type = "Ladder",
                Name = "",
                DisplayName = "",
                QualifiedItemId = "",
                ParentSheetIndex = -1,
                Source = "TileAction",
                Action = action,
            };
        }

        private static bool IsLadderObject(StardewValley.Object obj)
        {
            string name = obj.Name ?? "";
            string displayName = obj.DisplayName ?? "";
            string qualifiedItemId = obj.QualifiedItemId ?? "";
            string itemId = obj.ItemId ?? "";

            return obj.ParentSheetIndex == 173
                || string.Equals(qualifiedItemId, "(O)173", StringComparison.OrdinalIgnoreCase)
                || string.Equals(itemId, "173", StringComparison.OrdinalIgnoreCase)
                || name.IndexOf("Ladder", StringComparison.OrdinalIgnoreCase) >= 0
                || displayName.IndexOf("Ladder", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("Stairs", StringComparison.OrdinalIgnoreCase) >= 0
                || displayName.IndexOf("Stairs", StringComparison.OrdinalIgnoreCase) >= 0
                || qualifiedItemId.IndexOf("Ladder", StringComparison.OrdinalIgnoreCase) >= 0
                || qualifiedItemId.IndexOf("Stairs", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static List<object> CreateLadderBuildingTileSnapshots(GameLocation location, HashSet<string> seenTiles)
        {
            var ladderTiles = new List<object>();
            var currentMap = location.Map ?? location.map;
            if (currentMap == null)
            {
                return ladderTiles;
            }

            var buildingsLayer = currentMap.GetLayer("Buildings");
            if (buildingsLayer == null)
            {
                return ladderTiles;
            }

            for (int x = 0; x < buildingsLayer.LayerWidth; x++)
            {
                for (int y = 0; y < buildingsLayer.LayerHeight; y++)
                {
                    int? tileIndex = buildingsLayer.Tiles[x, y]?.TileIndex;
                    if (tileIndex != 173)
                    {
                        continue;
                    }

                    string tileKey = BuildTileKey(x, y);
                    if (seenTiles.Contains(tileKey))
                    {
                        continue;
                    }

                    seenTiles.Add(tileKey);
                    ladderTiles.Add(CreateLadderBuildingTileSnapshot(x, y));
                }
            }

            return ladderTiles;
        }

        private static List<object> CreateLadderActionTileSnapshots(GameLocation location, HashSet<string> seenTiles)
        {
            var ladderActionTiles = new List<object>();
            var currentMap = location.Map ?? location.map;
            if (currentMap == null || currentMap.Layers == null || currentMap.Layers.Count == 0)
            {
                return ladderActionTiles;
            }

            int width = currentMap.Layers[0].LayerWidth;
            int height = currentMap.Layers[0].LayerHeight;

            for (int x = 0; x < width; x++)
            {
                for (int y = 0; y < height; y++)
                {
                    string combined = ReadCombinedTileAction(location, x, y);
                    if (!LooksLikeNextLevelLadderAction(combined))
                    {
                        continue;
                    }

                    string tileKey = BuildTileKey(x, y);
                    if (seenTiles.Contains(tileKey))
                    {
                        continue;
                    }

                    seenTiles.Add(tileKey);
                    ladderActionTiles.Add(CreateLadderActionTileSnapshot(x, y, combined));
                }
            }

            return ladderActionTiles;
        }

        private static int? ReadBuildingTileIndex(GameLocation location, int tileX, int tileY)
        {
            var currentMap = location.Map ?? location.map;
            var buildingsLayer = currentMap?.GetLayer("Buildings");
            if (buildingsLayer == null)
            {
                return null;
            }

            if (tileX < 0 || tileY < 0 || tileX >= buildingsLayer.LayerWidth || tileY >= buildingsLayer.LayerHeight)
            {
                return null;
            }

            return buildingsLayer.Tiles[tileX, tileY]?.TileIndex;
        }

        private static string ReadCombinedTileAction(GameLocation location, int tileX, int tileY)
        {
            string action = location.doesTileHaveProperty(tileX, tileY, "Action", "Buildings") ?? "";
            string backAction = location.doesTileHaveProperty(tileX, tileY, "Action", "Back") ?? "";
            string touchAction = location.doesTileHaveProperty(tileX, tileY, "TouchAction", "Back") ?? "";
            return $"{action} {backAction} {touchAction}".Trim();
        }

        private static bool LooksLikeNextLevelLadderAction(string actionText)
        {
            if (string.IsNullOrWhiteSpace(actionText))
            {
                return false;
            }

            return actionText.IndexOf("Ladder", StringComparison.OrdinalIgnoreCase) >= 0
                || actionText.IndexOf("Stairs", StringComparison.OrdinalIgnoreCase) >= 0
                || actionText.IndexOf("NextMineLevel", StringComparison.OrdinalIgnoreCase) >= 0
                || actionText.IndexOf("MineShaft", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static bool IsMiningNodeObject(StardewValley.Object obj)
        {
            if (IsLadderObject(obj))
            {
                return false;
            }

            string name = obj.Name ?? "";
            string qualifiedItemId = obj.QualifiedItemId ?? "";
            return name.IndexOf("Stone", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("Ore", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("Boulder", StringComparison.OrdinalIgnoreCase) >= 0
                || qualifiedItemId.IndexOf("Stone", StringComparison.OrdinalIgnoreCase) >= 0
                || qualifiedItemId.IndexOf("Ore", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static bool IsMineShaftBreakableObject(StardewValley.Object obj)
        {
            if (IsLadderObject(obj))
            {
                return false;
            }

            if (obj is Chest)
            {
                return false;
            }

            string name = obj.Name ?? "";
            string displayName = obj.DisplayName ?? "";
            string qualifiedItemId = obj.QualifiedItemId ?? "";
            string combined = $"{name} {displayName} {qualifiedItemId}";
            if (combined.IndexOf("Torch", StringComparison.OrdinalIgnoreCase) >= 0
                || combined.IndexOf("Chest", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return false;
            }

            return true;
        }

        private static string GetMiningNodeType(StardewValley.Object obj)
        {
            string name = obj.Name ?? "";
            string qualifiedItemId = obj.QualifiedItemId ?? "";
            string combined = $"{name} {qualifiedItemId}";

            if (combined.IndexOf("Ore", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return "Ore";
            }
            if (combined.IndexOf("Boulder", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return "Boulder";
            }
            return "Stone";
        }

        private static bool LooksLikeMineEntranceAction(string actionText)
        {
            if (string.IsNullOrWhiteSpace(actionText))
            {
                return false;
            }

            return actionText.IndexOf("Mine", StringComparison.OrdinalIgnoreCase) >= 0
                || actionText.IndexOf("Shaft", StringComparison.OrdinalIgnoreCase) >= 0
                || actionText.IndexOf("Elevator", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static string BuildTileKey(int tileX, int tileY)
        {
            return $"{tileX},{tileY}";
        }
    }
}
