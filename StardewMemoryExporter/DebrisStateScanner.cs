using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using Microsoft.Xna.Framework;
using StardewValley;

namespace StardewMemoryExporter
{
    public static class DebrisStateScanner
    {
        private const int ObjectSpriteSize = 16;
        private const int MaxFilteredDebrisDebugKeys = 5000;
        private const int MaxMineDebrisInspectionDebugKeys = 5000;
        private const int MaxMemberDumpValueLength = 160;
        private const int MaxMemberDumpTotalLength = 6000;
        private const int WoodObjectId = 388;
        private const int TreeResourceDebrisTypeValue = 6;
        private const int TreeResourceChunkTypeValue = 0;

        private static readonly object DebugLogLock = new object();
        private static readonly HashSet<string> LoggedFilteredDebrisDebugKeys = new HashSet<string>();
        private static readonly HashSet<string> LoggedMineDebrisInspectionDebugKeys = new HashSet<string>();

        private static readonly HashSet<string> IgnoredDebrisQualifiedItemIds = new HashSet<string>
        {
            // Weeds 是杂草对象本体，不是 Agent 需要拾取的真实掉落物；真正有价值的是 Fiber / Mixed Seeds 等掉落。
            "(O)0",
        };

        public static List<object> CreateDebrisSnapshot(GameLocation location)
        {
            var debrisSnapshots = new List<object>();
            if (location == null)
            {
                return debrisSnapshots;
            }

            foreach (Debris debris in location.debris)
            {
                if (debris == null) continue;
                debrisSnapshots.AddRange(CreateDebrisObjectSnapshots(location, debris));
            }

            return debrisSnapshots;
        }

        private static List<object> CreateDebrisObjectSnapshots(GameLocation location, Debris debris)
        {
            Item item = ReadOptionalItemMember(debris, "item");
            string source = ReadOptionalStringMember(debris, "debrisType")
                ?? ReadOptionalStringMember(debris, "chunkType")
                ?? debris.GetType().Name;
            int debrisTypeValue = TryReadIntMember(debris, "debrisType", out int rawDebrisTypeValue)
                ? rawDebrisTypeValue
                : -1;
            int chunkTypeValue = TryReadIntMember(debris, "chunkType", out int rawChunkTypeValue)
                ? rawChunkTypeValue
                : -1;

            if (item != null)
            {
                Vector2 position = ReadDebrisPosition(debris);
                int stack = item.Stack;
                ResolvedDebrisItem resolvedItem = ResolveDebrisItem(item);
                WriteMineDebrisInspectionLog(
                    location,
                    debris,
                    null,
                    source,
                    debrisTypeValue,
                    chunkTypeValue,
                    chunkTypeValue,
                    resolvedItem,
                    "debris_item"
                );
                if (!HasCompleteItemIdentity(resolvedItem))
                {
                    WriteFilteredDebrisDebugLog(
                        location,
                        debris,
                        null,
                        source,
                        debrisTypeValue,
                        chunkTypeValue,
                        chunkTypeValue,
                        resolvedItem,
                        "debris_item_identity_incomplete"
                    );
                    return new List<object>();
                }

                return new List<object>
                {
                    CreateDebrisSnapshotObject(
                        resolvedItem,
                        stack,
                        position,
                        source,
                        true,
                        debrisTypeValue,
                        chunkTypeValue
                    )
                };
            }

            if (TryResolveDebrisItemId(debris, out Item itemIdItem))
            {
                Vector2 position = ReadDebrisPosition(debris);
                int stack = ReadDebrisChunkCount(debris);
                ResolvedDebrisItem resolvedItem = ResolveDebrisItem(itemIdItem);
                WriteMineDebrisInspectionLog(
                    location,
                    debris,
                    null,
                    source,
                    debrisTypeValue,
                    chunkTypeValue,
                    chunkTypeValue,
                    resolvedItem,
                    "debris_item_id"
                );
                if (!HasCompleteItemIdentity(resolvedItem))
                {
                    WriteFilteredDebrisDebugLog(
                        location,
                        debris,
                        null,
                        source,
                        debrisTypeValue,
                        chunkTypeValue,
                        chunkTypeValue,
                        resolvedItem,
                        "debris_item_id_identity_incomplete"
                    );
                    return new List<object>();
                }

                return new List<object>
                {
                    CreateDebrisSnapshotObject(
                        resolvedItem,
                        stack,
                        position,
                        source,
                        true,
                        debrisTypeValue,
                        chunkTypeValue
                    )
                };
            }

            List<object> chunkSnapshots = new List<object>();
            foreach (object chunk in ReadDebrisChunks(debris))
            {
                if (chunk == null) continue;

                Vector2 chunkPosition = ReadDebrisChunkPosition(chunk, debris);
                int chunkDebrisTypeValue = TryReadChunkDebrisType(chunk, out int rawChunkDebrisTypeValue)
                    ? rawChunkDebrisTypeValue
                    : -1;
                ResolvedDebrisItem resolvedItem = ResolveDebrisItemFromChunk(
                    chunk,
                    source,
                    debrisTypeValue,
                    chunkTypeValue,
                    chunkDebrisTypeValue,
                    out int resolvedChunkTypeValue
                );
                WriteMineDebrisInspectionLog(
                    location,
                    debris,
                    chunk,
                    source,
                    debrisTypeValue,
                    chunkTypeValue,
                    resolvedChunkTypeValue,
                    resolvedItem,
                    "chunk"
                );
                if (!HasCompleteItemIdentity(resolvedItem))
                {
                    WriteFilteredDebrisDebugLog(
                        location,
                        debris,
                        chunk,
                        source,
                        debrisTypeValue,
                        chunkTypeValue,
                        resolvedChunkTypeValue,
                        resolvedItem,
                        "chunk_identity_incomplete"
                    );
                    continue;
                }

                chunkSnapshots.Add(CreateDebrisSnapshotObject(
                    resolvedItem,
                    1,
                    chunkPosition,
                    source,
                    true,
                    debrisTypeValue,
                    resolvedChunkTypeValue >= 0 ? resolvedChunkTypeValue : chunkTypeValue
                ));
            }

            if (chunkSnapshots.Count > 0)
            {
                return chunkSnapshots;
            }

            Vector2 fallbackPosition = ReadDebrisPosition(debris);
            ResolvedDebrisItem fallbackItem = ResolveGenericDebrisItem(source);
            WriteMineDebrisInspectionLog(
                location,
                debris,
                null,
                source,
                debrisTypeValue,
                chunkTypeValue,
                chunkTypeValue,
                fallbackItem,
                "fallback"
            );
            if (!HasCompleteItemIdentity(fallbackItem))
            {
                WriteFilteredDebrisDebugLog(
                    location,
                    debris,
                    null,
                    source,
                    debrisTypeValue,
                    chunkTypeValue,
                    chunkTypeValue,
                    fallbackItem,
                    "fallback_identity_incomplete"
                );
                return new List<object>();
            }

            return new List<object>
            {
                CreateDebrisSnapshotObject(
                    fallbackItem,
                    ReadDebrisChunkCount(debris),
                    fallbackPosition,
                    source,
                    true,
                    debrisTypeValue,
                    chunkTypeValue
                )
            };
        }

        private static object CreateDebrisSnapshotObject(
            ResolvedDebrisItem resolvedItem,
            int stack,
            Vector2 position,
            string source,
            bool isCollectible,
            int debrisTypeValue,
            int chunkTypeValue
        )
        {
            int tileSize = Game1.tileSize;

            return new
            {
                Name = resolvedItem.Name,
                DisplayName = resolvedItem.DisplayName,
                QualifiedItemId = resolvedItem.QualifiedItemId,
                Category = resolvedItem.Category,
                Stack = Math.Max(stack, 1),
                Position = new[] { Math.Round((double)position.X, 1), Math.Round((double)position.Y, 1) },
                Tile = new[] { (int)(position.X / tileSize), (int)(position.Y / tileSize) },
                Source = source,
                IsCollectible = isCollectible,
                DebrisTypeValue = debrisTypeValue,
                ChunkTypeValue = chunkTypeValue,
            };
        }

        private static ResolvedDebrisItem ResolveDebrisItem(Item item)
        {
            return new ResolvedDebrisItem(
                item.Name ?? "",
                item.DisplayName ?? "",
                item.QualifiedItemId ?? "",
                item.Category
            );
        }

        private static bool TryResolveDebrisItemId(Debris debris, out Item item)
        {
            item = null;
            string itemId = ReadOptionalStringValueMember(debris, "itemId");
            if (string.IsNullOrWhiteSpace(itemId))
            {
                return false;
            }

            return TryCreateItem(itemId, out item);
        }

        private static ResolvedDebrisItem ResolveDebrisItemFromChunk(
            object chunk,
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int chunkDebrisTypeValue,
            out int resolvedChunkTypeValue
        )
        {
            Item chunkItem = ReadOptionalItemFromMembers(chunk, new[] { "item", "Item" });
            if (chunkItem != null)
            {
                resolvedChunkTypeValue = chunkDebrisTypeValue;
                return ResolveDebrisItem(chunkItem);
            }

            if (TryResolveWoodResourceDebrisChunk(
                source,
                debrisTypeValue,
                chunkTypeValue,
                chunkDebrisTypeValue,
                out Item woodItem
            ))
            {
                resolvedChunkTypeValue = WoodObjectId;
                return ResolveDebrisItem(woodItem);
            }

            if (chunkDebrisTypeValue >= 0
                && IsObjectLikeDebrisSource(source)
                && TryCreateObjectItem(chunkDebrisTypeValue, out Item item))
            {
                resolvedChunkTypeValue = chunkDebrisTypeValue;
                return ResolveDebrisItem(item);
            }

            if (IsObjectLikeDebrisSource(source)
                && TryResolveObjectItemFromSpriteSheet(chunk, out Item spriteSheetItem, out int spriteSheetObjectId))
            {
                resolvedChunkTypeValue = spriteSheetObjectId;
                return ResolveDebrisItem(spriteSheetItem);
            }

            resolvedChunkTypeValue = chunkDebrisTypeValue;
            return ResolveGenericDebrisItem(source);
        }

        private static bool TryResolveWoodResourceDebrisChunk(
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int chunkDebrisTypeValue,
            out Item woodItem
        )
        {
            woodItem = null;
            if (!string.Equals(source, "RESOURCE", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            if (debrisTypeValue != TreeResourceDebrisTypeValue || chunkTypeValue != TreeResourceChunkTypeValue)
            {
                return false;
            }

            // 树木资源 Debris 的 chunkNetDebrisType 不是物品 id；日志中稳定表现为 0/1。
            // 这里仅把这类 RESOURCE chunk 映射为真实可拾取木材，避免放宽 OBJECT/CHUNKS 视觉碎屑。
            if (chunkDebrisTypeValue != 0 && chunkDebrisTypeValue != 1)
            {
                return false;
            }

            return TryCreateObjectItem(WoodObjectId, out woodItem);
        }

        private static ResolvedDebrisItem ResolveGenericDebrisItem(string source)
        {
            return new ResolvedDebrisItem(source, source, "", 0);
        }

        private static bool HasCompleteItemIdentity(ResolvedDebrisItem resolvedItem)
        {
            return resolvedItem != null
                && !string.IsNullOrWhiteSpace(resolvedItem.QualifiedItemId)
                && !string.IsNullOrWhiteSpace(resolvedItem.Name)
                && !string.IsNullOrWhiteSpace(resolvedItem.DisplayName)
                && !IgnoredDebrisQualifiedItemIds.Contains(resolvedItem.QualifiedItemId);
        }

        private static bool IsObjectLikeDebrisSource(string source)
        {
            return string.Equals(source, "OBJECT", StringComparison.OrdinalIgnoreCase)
                || string.Equals(source, "RESOURCE", StringComparison.OrdinalIgnoreCase);
        }

        private static bool TryCreateObjectItem(int objectId, out Item item)
        {
            item = null;
            if (objectId < 0)
            {
                return false;
            }

            string qualifiedItemId = $"(O){objectId}";
            return TryCreateItem(qualifiedItemId, out item);
        }

        private static bool TryCreateItem(string qualifiedItemId, out Item item)
        {
            item = null;
            if (string.IsNullOrWhiteSpace(qualifiedItemId) || !qualifiedItemId.StartsWith("(", StringComparison.Ordinal))
            {
                return false;
            }

            try
            {
                item = ItemRegistry.Create(qualifiedItemId);
                return item != null
                    && string.Equals(item.QualifiedItemId, qualifiedItemId, StringComparison.OrdinalIgnoreCase)
                    && !string.Equals(item.Name, "Error Item", StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                item = null;
                return false;
            }
        }

        private static bool TryResolveObjectItemFromSpriteSheet(object chunk, out Item item, out int objectId)
        {
            item = null;
            objectId = -1;

            if (!TryReadIntMember(chunk, "xSpriteSheet", out int xSpriteSheet)
                || !TryReadIntMember(chunk, "ySpriteSheet", out int ySpriteSheet))
            {
                return false;
            }

            int columns = ReadObjectSpriteSheetColumns();
            if (columns <= 0)
            {
                return false;
            }

            foreach (int candidateObjectId in BuildObjectIdCandidates(xSpriteSheet, ySpriteSheet, columns))
            {
                if (TryCreateObjectItem(candidateObjectId, out Item candidateItem))
                {
                    item = candidateItem;
                    objectId = candidateObjectId;
                    return true;
                }
            }

            return false;
        }

        private static int ReadObjectSpriteSheetColumns()
        {
            try
            {
                if (Game1.objectSpriteSheet == null || Game1.objectSpriteSheet.Width <= 0)
                {
                    return 0;
                }

                return Game1.objectSpriteSheet.Width / ObjectSpriteSize;
            }
            catch
            {
                return 0;
            }
        }

        private static IEnumerable<int> BuildObjectIdCandidates(int xSpriteSheet, int ySpriteSheet, int columns)
        {
            if (xSpriteSheet < 0 || ySpriteSheet < 0 || columns <= 0)
            {
                yield break;
            }

            // 某些 Debris chunk 记录的是 object sprite sheet 的格子坐标。
            yield return xSpriteSheet + ySpriteSheet * columns;

            // 另一些 Debris chunk 记录的是 sprite sheet 像素坐标；这里转换为物品表索引。
            int pixelBasedObjectId = (xSpriteSheet / ObjectSpriteSize) + (ySpriteSheet / ObjectSpriteSize) * columns;
            if (pixelBasedObjectId != xSpriteSheet + ySpriteSheet * columns)
            {
                yield return pixelBasedObjectId;
            }
        }

        private static void WriteFilteredDebrisDebugLog(
            GameLocation location,
            Debris debris,
            object chunk,
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            ResolvedDebrisItem resolvedItem,
            string reason
        )
        {
            try
            {
                string locationName = location?.NameOrUniqueName ?? "";
                Vector2 position = chunk != null ? ReadDebrisChunkPosition(chunk, debris) : ReadDebrisPosition(debris);
                int tileSize = Game1.tileSize;
                int tileX = tileSize > 0 ? (int)(position.X / tileSize) : -1;
                int tileY = tileSize > 0 ? (int)(position.Y / tileSize) : -1;
                string debugKey = BuildFilteredDebrisDebugKey(
                    locationName,
                    source,
                    reason,
                    debrisTypeValue,
                    chunkTypeValue,
                    resolvedChunkTypeValue,
                    chunk,
                    tileX,
                    tileY
                );

                lock (DebugLogLock)
                {
                    if (LoggedFilteredDebrisDebugKeys.Count > MaxFilteredDebrisDebugKeys)
                    {
                        LoggedFilteredDebrisDebugKeys.Clear();
                    }

                    if (!LoggedFilteredDebrisDebugKeys.Add(debugKey))
                    {
                        return;
                    }

                    string logDirectory = ResolveDebugLogDirectory();
                    Directory.CreateDirectory(logDirectory);
                    File.AppendAllText(
                        Path.Combine(logDirectory, "csharp_debris_debug.log"),
                        BuildFilteredDebrisDebugLine(
                            locationName,
                            debris,
                            chunk,
                            source,
                            debrisTypeValue,
                            chunkTypeValue,
                            resolvedChunkTypeValue,
                            resolvedItem,
                            reason,
                            position,
                            tileX,
                            tileY
                        ) + Environment.NewLine
                    );
                }
            }
            catch
            {
                // Debris 调试日志不能影响 Observer 高频 state 输出。
            }
        }

        private static void WriteMineDebrisInspectionLog(
            GameLocation location,
            Debris debris,
            object chunk,
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            ResolvedDebrisItem resolvedItem,
            string inspectPhase
        )
        {
            try
            {
                if (!IsMineLocation(location))
                {
                    return;
                }

                string locationName = location?.NameOrUniqueName ?? "";
                Vector2 position = chunk != null ? ReadDebrisChunkPosition(chunk, debris) : ReadDebrisPosition(debris);
                int tileSize = Game1.tileSize;
                int tileX = tileSize > 0 ? (int)(position.X / tileSize) : -1;
                int tileY = tileSize > 0 ? (int)(position.Y / tileSize) : -1;
                string debugKey = BuildMineDebrisInspectionDebugKey(
                    locationName,
                    source,
                    inspectPhase,
                    debrisTypeValue,
                    chunkTypeValue,
                    resolvedChunkTypeValue,
                    resolvedItem,
                    chunk,
                    tileX,
                    tileY
                );

                lock (DebugLogLock)
                {
                    if (LoggedMineDebrisInspectionDebugKeys.Count > MaxMineDebrisInspectionDebugKeys)
                    {
                        LoggedMineDebrisInspectionDebugKeys.Clear();
                    }

                    if (!LoggedMineDebrisInspectionDebugKeys.Add(debugKey))
                    {
                        return;
                    }

                    string logDirectory = ResolveDebugLogDirectory();
                    Directory.CreateDirectory(logDirectory);
                    File.AppendAllText(
                        Path.Combine(logDirectory, "csharp_mine_debris_inspection.log"),
                        BuildMineDebrisInspectionDebugLine(
                            locationName,
                            debris,
                            chunk,
                            source,
                            debrisTypeValue,
                            chunkTypeValue,
                            resolvedChunkTypeValue,
                            resolvedItem,
                            inspectPhase,
                            position,
                            tileX,
                            tileY
                        ) + Environment.NewLine
                    );
                }
            }
            catch
            {
                // 矿井 Debris 诊断日志不能影响 Observer 高频 state 输出。
            }
        }

        private static bool IsMineLocation(GameLocation location)
        {
            if (location == null)
            {
                return false;
            }

            string locationName = location.NameOrUniqueName ?? "";
            return location.GetType().Name == "MineShaft"
                || locationName.StartsWith("UndergroundMine", StringComparison.OrdinalIgnoreCase);
        }

        private static string BuildMineDebrisInspectionDebugKey(
            string locationName,
            string source,
            string inspectPhase,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            ResolvedDebrisItem resolvedItem,
            object chunk,
            int tileX,
            int tileY
        )
        {
            return string.Join(
                "|",
                locationName,
                source,
                inspectPhase,
                debrisTypeValue,
                chunkTypeValue,
                resolvedChunkTypeValue,
                resolvedItem?.QualifiedItemId ?? "",
                resolvedItem?.Name ?? "",
                ReadDebugIntMember(chunk, "netDebrisType"),
                ReadDebugIntMember(chunk, "xSpriteSheet"),
                ReadDebugIntMember(chunk, "ySpriteSheet"),
                tileX,
                tileY
            );
        }

        private static string BuildMineDebrisInspectionDebugLine(
            string locationName,
            Debris debris,
            object chunk,
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            ResolvedDebrisItem resolvedItem,
            string inspectPhase,
            Vector2 position,
            int tileX,
            int tileY
        )
        {
            Item debrisItem = ReadOptionalItemMember(debris, "item");
            Item chunkItem = ReadOptionalItemFromMembers(chunk, new[] { "item", "Item" });
            bool hasCompleteItemIdentity = HasCompleteItemIdentity(resolvedItem);

            StringBuilder sb = new StringBuilder();
            sb.Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff"));
            sb.Append(" [DebrisStateScanner] mine_debris_inspection");
            sb.Append($" phase={inspectPhase}");
            sb.Append($" location={locationName}");
            sb.Append($" source={source}");
            sb.Append($" debris_type={debris?.GetType().FullName ?? ""}");
            sb.Append($" chunk_type={chunk?.GetType().FullName ?? ""}");
            sb.Append($" debris_has_item={debrisItem != null}");
            sb.Append($" debris_item={FormatItemDebugText(debrisItem)}");
            sb.Append($" chunk_has_item={chunkItem != null}");
            sb.Append($" chunk_item={FormatItemDebugText(chunkItem)}");
            sb.Append($" debrisTypeValue={debrisTypeValue}");
            sb.Append($" chunkTypeValue={chunkTypeValue}");
            sb.Append($" resolvedChunkTypeValue={resolvedChunkTypeValue}");
            sb.Append($" chunkNetDebrisType={ReadDebugIntMember(chunk, "netDebrisType")}");
            sb.Append($" chunkDebrisType={ReadDebugIntMember(chunk, "debrisType")}");
            sb.Append($" chunkChunkType={ReadDebugIntMember(chunk, "chunkType")}");
            sb.Append($" xSpriteSheet={ReadDebugIntMember(chunk, "xSpriteSheet")}");
            sb.Append($" ySpriteSheet={ReadDebugIntMember(chunk, "ySpriteSheet")}");
            sb.Append($" objectSpriteColumns={ReadObjectSpriteSheetColumns()}");
            sb.Append($" objectIdCandidates=[{BuildObjectIdCandidateDebugText(chunk)}]");
            sb.Append($" position=({position.X:F1},{position.Y:F1})");
            sb.Append($" tile=({tileX},{tileY})");
            sb.Append($" resolvedName={resolvedItem?.Name ?? ""}");
            sb.Append($" resolvedDisplayName={resolvedItem?.DisplayName ?? ""}");
            sb.Append($" resolvedQualifiedItemId={resolvedItem?.QualifiedItemId ?? ""}");
            sb.Append($" resolvedCategory={resolvedItem?.Category.ToString() ?? ""}");
            sb.Append($" hasCompleteItemIdentity={hasCompleteItemIdentity}");
            sb.Append($" debris_members=[{BuildMemberDumpText(debris)}]");
            sb.Append($" chunk_members=[{BuildMemberDumpText(chunk)}]");
            return sb.ToString();
        }

        private static string FormatItemDebugText(Item item)
        {
            if (item == null)
            {
                return "";
            }

            return $"{item.Name}/{item.DisplayName}/{item.QualifiedItemId}/category={item.Category}/stack={item.Stack}";
        }

        private static string BuildMemberDumpText(object source)
        {
            if (source == null)
            {
                return "";
            }

            try
            {
                BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
                Type sourceType = source.GetType();
                List<string> memberTexts = new List<string>();

                foreach (FieldInfo fieldInfo in sourceType.GetFields(flags))
                {
                    AppendMemberDumpText(memberTexts, fieldInfo.Name, SafeReadMemberValue(() => fieldInfo.GetValue(source)));
                }

                foreach (PropertyInfo propertyInfo in sourceType.GetProperties(flags))
                {
                    if (propertyInfo.GetIndexParameters().Length > 0)
                    {
                        continue;
                    }

                    AppendMemberDumpText(memberTexts, propertyInfo.Name, SafeReadMemberValue(() => propertyInfo.GetValue(source)));
                }

                string text = string.Join(", ", memberTexts);
                return TruncateDebugText(text, MaxMemberDumpTotalLength);
            }
            catch (Exception ex)
            {
                return $"<dump_error:{ex.GetType().Name}:{ex.Message}>";
            }
        }

        private static object SafeReadMemberValue(Func<object> readValue)
        {
            try
            {
                return readValue();
            }
            catch (Exception ex)
            {
                return $"<read_error:{ex.GetType().Name}>";
            }
        }

        private static void AppendMemberDumpText(List<string> memberTexts, string memberName, object value)
        {
            memberTexts.Add($"{memberName}={FormatMemberDumpValue(value)}");
        }

        private static string FormatMemberDumpValue(object value)
        {
            if (value == null)
            {
                return "null";
            }

            if (value is Item item)
            {
                return FormatItemDebugText(item);
            }

            if (value is Vector2 vector)
            {
                return $"({vector.X:F1},{vector.Y:F1})";
            }

            object wrappedValue = ReadOptionalMember(value, "Value");
            if (wrappedValue != null && !ReferenceEquals(wrappedValue, value))
            {
                return TruncateDebugText($"{value.GetType().Name}.Value={FormatSimpleMemberDumpValue(wrappedValue)}", MaxMemberDumpValueLength);
            }

            return TruncateDebugText(FormatSimpleMemberDumpValue(value), MaxMemberDumpValueLength);
        }

        private static string FormatSimpleMemberDumpValue(object value)
        {
            if (value == null)
            {
                return "null";
            }

            if (value is string text)
            {
                return text.Replace("\n", "\\n").Replace("\r", "\\r");
            }

            if (value is IEnumerable enumerable && !(value is string))
            {
                int count = 0;
                foreach (object _ in enumerable)
                {
                    count++;
                    if (count > 20)
                    {
                        return $"{value.GetType().Name}(count>20)";
                    }
                }

                return $"{value.GetType().Name}(count={count})";
            }

            return value.ToString()?.Replace("\n", "\\n").Replace("\r", "\\r") ?? "";
        }

        private static string TruncateDebugText(string text, int maxLength)
        {
            if (string.IsNullOrEmpty(text) || text.Length <= maxLength)
            {
                return text ?? "";
            }

            return text.Substring(0, maxLength) + "...<truncated>";
        }

        private static string ResolveDebugLogDirectory()
        {
            string configuredLogDirectory = Environment.GetEnvironmentVariable("VALLEY_AGENT_LOG_DIR");
            if (!string.IsNullOrWhiteSpace(configuredLogDirectory))
            {
                return configuredLogDirectory;
            }

            string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (!string.IsNullOrWhiteSpace(userProfile))
            {
                return Path.Combine(userProfile, "Desktop", "valley-agent", "logs");
            }

            return Path.Combine(Environment.CurrentDirectory, "logs");
        }

        private static string BuildFilteredDebrisDebugKey(
            string locationName,
            string source,
            string reason,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            object chunk,
            int tileX,
            int tileY
        )
        {
            return string.Join(
                "|",
                locationName,
                source,
                reason,
                debrisTypeValue,
                chunkTypeValue,
                resolvedChunkTypeValue,
                ReadDebugIntMember(chunk, "netDebrisType"),
                ReadDebugIntMember(chunk, "xSpriteSheet"),
                ReadDebugIntMember(chunk, "ySpriteSheet"),
                tileX,
                tileY
            );
        }

        private static string BuildFilteredDebrisDebugLine(
            string locationName,
            Debris debris,
            object chunk,
            string source,
            int debrisTypeValue,
            int chunkTypeValue,
            int resolvedChunkTypeValue,
            ResolvedDebrisItem resolvedItem,
            string reason,
            Vector2 position,
            int tileX,
            int tileY
        )
        {
            StringBuilder sb = new StringBuilder();
            sb.Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff"));
            sb.Append(" [DebrisStateScanner] filtered_debris");
            sb.Append($" reason={reason}");
            sb.Append($" location={locationName}");
            sb.Append($" source={source}");
            sb.Append($" debris_type={debris?.GetType().FullName ?? ""}");
            sb.Append($" chunk_type={chunk?.GetType().FullName ?? ""}");
            sb.Append($" debris_has_item={ReadOptionalItemMember(debris, "item") != null}");
            sb.Append($" chunk_has_item={ReadOptionalItemFromMembers(chunk, new[] { "item", "Item" }) != null}");
            sb.Append($" debrisTypeValue={debrisTypeValue}");
            sb.Append($" chunkTypeValue={chunkTypeValue}");
            sb.Append($" resolvedChunkTypeValue={resolvedChunkTypeValue}");
            sb.Append($" chunkNetDebrisType={ReadDebugIntMember(chunk, "netDebrisType")}");
            sb.Append($" chunkDebrisType={ReadDebugIntMember(chunk, "debrisType")}");
            sb.Append($" chunkChunkType={ReadDebugIntMember(chunk, "chunkType")}");
            sb.Append($" xSpriteSheet={ReadDebugIntMember(chunk, "xSpriteSheet")}");
            sb.Append($" ySpriteSheet={ReadDebugIntMember(chunk, "ySpriteSheet")}");
            sb.Append($" objectSpriteColumns={ReadObjectSpriteSheetColumns()}");
            sb.Append($" objectIdCandidates=[{BuildObjectIdCandidateDebugText(chunk)}]");
            sb.Append($" position=({position.X:F1},{position.Y:F1})");
            sb.Append($" tile=({tileX},{tileY})");
            sb.Append($" resolvedName={resolvedItem?.Name ?? ""}");
            sb.Append($" resolvedDisplayName={resolvedItem?.DisplayName ?? ""}");
            sb.Append($" resolvedQualifiedItemId={resolvedItem?.QualifiedItemId ?? ""}");
            sb.Append($" resolvedCategory={resolvedItem?.Category.ToString() ?? ""}");
            return sb.ToString();
        }

        private static string BuildObjectIdCandidateDebugText(object chunk)
        {
            if (chunk == null
                || !TryReadIntMember(chunk, "xSpriteSheet", out int xSpriteSheet)
                || !TryReadIntMember(chunk, "ySpriteSheet", out int ySpriteSheet))
            {
                return "";
            }

            int columns = ReadObjectSpriteSheetColumns();
            List<string> candidates = new List<string>();
            foreach (int objectId in BuildObjectIdCandidates(xSpriteSheet, ySpriteSheet, columns))
            {
                string itemName = TryCreateObjectItem(objectId, out Item item)
                    ? $"{item.Name}/{item.DisplayName}/{item.QualifiedItemId}"
                    : "unresolved";
                candidates.Add($"{objectId}:{itemName}");
            }

            return string.Join(",", candidates);
        }

        private static string ReadDebugIntMember(object source, string memberName)
        {
            return TryReadIntMember(source, memberName, out int value) ? value.ToString() : "";
        }

        private static Vector2 ReadDebrisPosition(Debris debris)
        {
            foreach (object chunk in ReadDebrisChunks(debris))
            {
                if (chunk == null) continue;
                if (TryReadVector2FromMembers(
                    chunk,
                    new[] { "position", "Position", "chunkPosition", "ChunkPosition" },
                    out Vector2 chunkPosition
                ))
                {
                    return chunkPosition;
                }
            }

            if (TryReadVector2FromMembers(
                debris,
                new[] { "position", "Position", "chunkPosition", "ChunkPosition" },
                out Vector2 debrisPosition
            ))
            {
                return debrisPosition;
            }

            return Vector2.Zero;
        }

        private static Vector2 ReadDebrisChunkPosition(object chunk, Debris fallbackDebris)
        {
            if (TryReadVector2FromMembers(
                chunk,
                new[] { "position", "Position", "chunkPosition", "ChunkPosition" },
                out Vector2 chunkPosition
            ))
            {
                return chunkPosition;
            }

            return ReadDebrisPosition(fallbackDebris);
        }

        private static bool TryReadVector2FromMembers(object source, string[] memberNames, out Vector2 vector)
        {
            foreach (string memberName in memberNames)
            {
                object value = ReadOptionalMember(source, memberName);
                if (TryReadVector2(value, out vector))
                {
                    return true;
                }
            }

            vector = Vector2.Zero;
            return false;
        }

        private static bool TryReadVector2(object value, out Vector2 vector)
        {
            if (value is Vector2 directVector)
            {
                vector = directVector;
                return true;
            }

            object wrappedValue = ReadOptionalMember(value, "Value");
            if (wrappedValue is Vector2 wrappedVector)
            {
                vector = wrappedVector;
                return true;
            }

            if (TryReadFloatMember(value, "X", out float x) && TryReadFloatMember(value, "Y", out float y))
            {
                vector = new Vector2(x, y);
                return true;
            }

            vector = Vector2.Zero;
            return false;
        }

        private static bool TryReadFloatMember(object source, string memberName, out float result)
        {
            object value = ReadOptionalMember(source, memberName);
            if (value is float floatValue)
            {
                result = floatValue;
                return true;
            }

            if (value is double doubleValue)
            {
                result = (float)doubleValue;
                return true;
            }

            if (value is int intValue)
            {
                result = intValue;
                return true;
            }

            result = 0f;
            return false;
        }

        private static int ReadDebrisChunkCount(Debris debris)
        {
            int count = ReadDebrisChunks(debris).Count;
            return Math.Max(count, 1);
        }

        private static List<object> ReadDebrisChunks(Debris debris)
        {
            List<object> result = new List<object>();
            object chunks = ReadOptionalMember(debris, "chunks");
            if (chunks is IEnumerable chunkEnumerable)
            {
                foreach (object chunk in chunkEnumerable)
                {
                    result.Add(chunk);
                }
            }

            return result;
        }

        private static bool TryReadChunkDebrisType(object chunk, out int result)
        {
            return TryReadIntMember(chunk, "netDebrisType", out result)
                || TryReadIntMember(chunk, "debrisType", out result)
                || TryReadIntMember(chunk, "chunkType", out result);
        }

        private static bool TryReadIntMember(object source, string memberName, out int result)
        {
            object value = ReadOptionalMember(source, memberName);
            if (TryReadInt(value, out result))
            {
                return true;
            }

            object wrappedValue = ReadOptionalMember(value, "Value");
            return TryReadInt(wrappedValue, out result);
        }

        private static bool TryReadInt(object value, out int result)
        {
            if (value is int intValue)
            {
                result = intValue;
                return true;
            }

            if (value is Enum enumValue)
            {
                result = Convert.ToInt32(enumValue);
                return true;
            }

            result = 0;
            return false;
        }

        private static Item ReadOptionalItemMember(object source, string memberName)
        {
            return ReadOptionalMember(source, memberName) as Item;
        }

        private static Item ReadOptionalItemFromMembers(object source, string[] memberNames)
        {
            foreach (string memberName in memberNames)
            {
                Item item = ReadOptionalItemMember(source, memberName);
                if (item != null)
                {
                    return item;
                }
            }

            return null;
        }

        private static string ReadOptionalStringMember(object source, string memberName)
        {
            object value = ReadOptionalMember(source, memberName);
            return value?.ToString();
        }

        private static string ReadOptionalStringValueMember(object source, string memberName)
        {
            object value = ReadOptionalMember(source, memberName);
            if (value is string text)
            {
                return text;
            }

            object wrappedValue = ReadOptionalMember(value, "Value");
            if (wrappedValue is string wrappedText)
            {
                return wrappedText;
            }

            return "";
        }

        private static object ReadOptionalMember(object source, string memberName)
        {
            if (source == null) return null;

            BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            Type sourceType = source.GetType();
            PropertyInfo propertyInfo = sourceType.GetProperty(memberName, flags);
            if (propertyInfo != null)
            {
                return propertyInfo.GetValue(source);
            }

            FieldInfo fieldInfo = sourceType.GetField(memberName, flags);
            if (fieldInfo != null)
            {
                return fieldInfo.GetValue(source);
            }

            return null;
        }

        private class ResolvedDebrisItem
        {
            public ResolvedDebrisItem(string name, string displayName, string qualifiedItemId, int category)
            {
                Name = name;
                DisplayName = displayName;
                QualifiedItemId = qualifiedItemId;
                Category = category;
            }

            public string Name { get; }
            public string DisplayName { get; }
            public string QualifiedItemId { get; }
            public int Category { get; }
        }
    }
}
