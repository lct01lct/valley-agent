using System;
using StardewModdingAPI;
using StardewModdingAPI.Events;

namespace StardewMemoryExporter
{
    public class ModEntry : Mod
    {
        private StardewObserver _observer;
        private StardewExecutor _executor;

        public override void Entry(IModHelper helper)
        {
            _observer = new StardewObserver(this.Monitor, 9999);
            _executor = new StardewExecutor(this.Monitor, this.Helper, 8888);

            helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
            helper.Events.GameLoop.SaveLoaded += (s, a) => this.Monitor.Log("🎮 存档载入，agent 接口链路完全激活！", LogLevel.Info);
        }

        private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
        {
            _observer.PulseGameMemory();
            _executor.UpdateMovementTick();
        }

        protected override void Dispose(bool disposing)
        {
            _observer?.CleanUp();
            _executor?.CleanUp();
            base.Dispose(disposing);
        }
    }
}