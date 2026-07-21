using System;
using StardewModdingAPI;
using StardewModdingAPI.Events;

namespace StardewMemoryExporter
{
    public class ModEntry : Mod
    {
        private StardewObserver _observer;
        private StardewExecutor _executor;

        private SharedBlackboard _blackboard;
        private bool _isShuttingDown = false;


        public override void Entry(IModHelper helper)
        {
            _blackboard = new SharedBlackboard();
            _observer = new StardewObserver(this.Monitor, _blackboard, 9999);
            _executor = new StardewExecutor(this.Monitor, _blackboard, this.Helper, 8888);

            AppDomain.CurrentDomain.ProcessExit += OnProcessExit;

            helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
            helper.Events.GameLoop.SaveLoaded += (s, a) => this.Monitor.Log("🎮 存档载入，agent 接口链路完全激活！", LogLevel.Info);
        }

        private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
        {
            if (_isShuttingDown) return;

            _observer.PulseGameMemory();
            // _observer.ListenHudMessages(e);
            _executor.ApplyHeldMove();
            _executor.ListenDialogMessages(e);
        }

        private void OnProcessExit(object sender, EventArgs e)
        {
            Shutdown();
        }

        private void Shutdown()
        {
            if (_isShuttingDown) return;
            _isShuttingDown = true;

            _observer?.CleanUp();
            _executor?.CleanUp();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                this.Helper.Events.GameLoop.UpdateTicked -= OnUpdateTicked;
                AppDomain.CurrentDomain.ProcessExit -= OnProcessExit;
            }

            Shutdown();
            base.Dispose(disposing);
        }
    }
}
