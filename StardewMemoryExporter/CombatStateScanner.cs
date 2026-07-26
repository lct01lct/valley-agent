using System;
using System.Collections.Generic;
using System.Reflection;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Monsters;

namespace StardewMemoryExporter
{
    internal static class CombatStateScanner
    {
        public static List<object> CreateMonstersSnapshot(GameLocation location, Farmer player)
        {
            var monsters = new List<object>();
            if (location == null || player == null)
            {
                return monsters;
            }

            foreach (NPC character in location.characters)
            {
                if (character is not Monster monster)
                {
                    continue;
                }

                bool isDead = ReadOptionalBoolMember(monster, "IsMonsterDead") ?? monster.Health <= 0;
                bool isInvisible = ReadOptionalBoolMember(monster, "IsInvisible") ?? false;
                Point monsterTile = monster.TilePoint;
                double distanceToPlayer = Math.Abs(monsterTile.X - player.TilePoint.X) + Math.Abs(monsterTile.Y - player.TilePoint.Y);

                monsters.Add(new
                {
                    Name = monster.Name ?? "",
                    DisplayName = ReadOptionalStringMember(monster, "DisplayName") ?? monster.Name ?? "",
                    Position = new[]
                    {
                        Math.Round((double)monster.Position.X, 1),
                        Math.Round((double)monster.Position.Y, 1),
                    },
                    Tile = new[] { monsterTile.X, monsterTile.Y },
                    Health = monster.Health,
                    MaxHealth = ReadOptionalIntMember(monster, "MaxHealth"),
                    DamageToFarmer = ReadOptionalIntMember(monster, "DamageToFarmer"),
                    SearchArraySize = ReadSearchArraySize(monster),
                    FocusedOnFarmer = ReadOptionalBoolMember(monster, "focusedOnFarmer") ?? false,
                    IsInvisible = isInvisible,
                    IsDead = isDead,
                    DistanceToPlayer = Math.Round(distanceToPlayer, 2),
                });
            }

            return monsters;
        }

        private static int? ReadOptionalIntMember(object source, string memberName)
        {
            object value = ReadOptionalMember(source, memberName);
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

        private static int ReadSearchArraySize(Monster monster)
        {
            foreach (string memberName in new[] { "getSearchArraySize", "GetSearchArraySize", "SearchArraySize" })
            {
                int? value = ReadOptionalIntMember(monster, memberName);
                if (value.HasValue && value.Value > 0)
                {
                    return value.Value;
                }
            }

            return 8;
        }

        private static bool? ReadOptionalBoolMember(object source, string memberName)
        {
            object value = ReadOptionalMember(source, memberName);
            if (value == null) return null;
            if (value is bool boolValue) return boolValue;

            PropertyInfo valueProperty = value.GetType().GetProperty("Value");
            if (valueProperty != null)
            {
                object innerValue = valueProperty.GetValue(value);
                if (innerValue is bool innerBoolValue) return innerBoolValue;
            }

            return null;
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

            MethodInfo methodInfo = sourceType.GetMethod(memberName, flags, Type.EmptyTypes);
            if (methodInfo != null)
            {
                return methodInfo.Invoke(source, Array.Empty<object>());
            }

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
