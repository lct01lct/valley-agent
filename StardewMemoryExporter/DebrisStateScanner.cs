using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using Microsoft.Xna.Framework;
using StardewValley;

namespace StardewMemoryExporter
{
    public static class DebrisStateScanner
    {
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
                debrisSnapshots.Add(CreateDebrisObjectSnapshot(debris));
            }

            return debrisSnapshots;
        }

        private static object CreateDebrisObjectSnapshot(Debris debris)
        {
            Item item = ReadOptionalItemMember(debris, "item");
            Vector2 position = ReadDebrisPosition(debris);
            int stack = item?.Stack ?? ReadDebrisChunkCount(debris);
            string source = ReadOptionalStringMember(debris, "debrisType")
                ?? ReadOptionalStringMember(debris, "chunkType")
                ?? debris.GetType().Name;
            bool isCollectible = IsCollectibleDebris(item, source);

            return new
            {
                Name = item?.Name ?? source,
                DisplayName = item?.DisplayName ?? source,
                QualifiedItemId = item?.QualifiedItemId ?? "",
                Category = item?.Category ?? 0,
                Stack = stack,
                Position = new[] { Math.Round((double)position.X, 1), Math.Round((double)position.Y, 1) },
                Tile = new[] { (int)(position.X / Game1.tileSize), (int)(position.Y / Game1.tileSize) },
                Source = source,
                IsCollectible = isCollectible,
            };
        }

        private static bool IsCollectibleDebris(Item item, string source)
        {
            if (item != null && !string.IsNullOrEmpty(item.QualifiedItemId))
            {
                return true;
            }

            // CHUNKS 是碎石/挥砍等纯视觉碎屑，不应触发 Python 端自动拾取。
            if (string.Equals(source, "CHUNKS", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            // 树木等掉落在 SMAPI Debris 中可能表现为 RESOURCE / OBJECT，且反射读不到 item。
            return string.Equals(source, "RESOURCE", StringComparison.OrdinalIgnoreCase)
                || string.Equals(source, "OBJECT", StringComparison.OrdinalIgnoreCase);
        }

        private static Vector2 ReadDebrisPosition(Debris debris)
        {
            object chunks = ReadOptionalMember(debris, "chunks");
            if (chunks is IEnumerable chunkEnumerable)
            {
                foreach (object chunk in chunkEnumerable)
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
            object chunks = ReadOptionalMember(debris, "chunks");
            if (chunks is ICollection collection)
            {
                return collection.Count;
            }

            if (chunks is IEnumerable chunkEnumerable)
            {
                int count = 0;
                foreach (object _ in chunkEnumerable)
                {
                    count++;
                }
                return count;
            }

            return 1;
        }

        private static Item ReadOptionalItemMember(object source, string memberName)
        {
            return ReadOptionalMember(source, memberName) as Item;
        }

        private static string ReadOptionalStringMember(object source, string memberName)
        {
            object value = ReadOptionalMember(source, memberName);
            return value?.ToString();
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
    }
}
