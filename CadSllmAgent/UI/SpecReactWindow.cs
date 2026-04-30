using System;
using System.IO;
using System.Text.Json;
using System.Windows;
using Microsoft.Web.WebView2.Wpf;
using Microsoft.Web.WebView2.Core;

namespace CadSllmAgent.UI
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class SpecReactWindow : Window
    {
        private WebView2 _webView;
        private string _viewType;

        public SpecReactWindow(string viewType = "spec")
        {
            _viewType = viewType;
            this.Title = viewType == "api" ? "API Key 관리 (API Key Management)" : "시방서 관리 (Specification Management)";
            
            // ★ viewType에 따라 창 크기 최적화
            if (viewType == "api")
            {
                this.Width = 550;
                this.Height = 600;
            }
            else
            {
                this.Width = 800;
                this.Height = 600;
            }

            this.ResizeMode = ResizeMode.NoResize;
            this.Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromRgb(30, 30, 36));

            // ★ [디자인 개선] 상단 흰색 타이틀 바 및 테두리 제거
            this.WindowStyle = WindowStyle.None;
            this.AllowsTransparency = true;
            this.BorderThickness = new Thickness(0);

            _webView = new WebView2();
            this.Content = _webView;

            this.Loaded += SpecReactWindow_Loaded;
        }

        // 창이 화면에 나타난 직후에 실행되는 부분
        private async void SpecReactWindow_Loaded(object sender, RoutedEventArgs e)
        {
            try
            {
                // ★ [정밀 위치 보정] 
                // WindowStartupLocation.CenterScreen 대신 수동으로 현재 모니터 중앙 계산
                double screenWidth = SystemParameters.PrimaryScreenWidth;
                double screenHeight = SystemParameters.PrimaryScreenHeight;
                
                // 사용자가 중앙보다 약간 왼쪽을 선호하므로 정중앙에서 -50px 보정
                this.Left = ((screenWidth - this.Width) / 2) - 50;
                this.Top = (screenHeight - this.Height) / 2;

                string folder = Path.Combine(Path.GetTempPath(), "CadSllmAgent_WebView2");
                var env = await CoreWebView2Environment.CreateAsync(null, folder, null);
                await _webView.EnsureCoreWebView2Async(env);

                // ★ 전달받은 viewType에 따라 URL 동적 결정
                _webView.CoreWebView2.Navigate($"{AgentConfig.FrontendBaseUrl}?view={_viewType}");

                // 리액트에서 전송되는 신호 처리
                _webView.CoreWebView2.WebMessageReceived += (s, args) =>
                {
                    string message = args.TryGetWebMessageAsString();
                    if (message == "CLOSE_MODAL")
                    {
                        this.Close();
                        return;
                    }
                    try
                    {
                        using var doc = JsonDocument.Parse(message);
                        if (!doc.RootElement.TryGetProperty("action", out var actionEl)) return;
                        var action = actionEl.GetString();
                        if (action == "TEMP_SPEC_SELECTION" && doc.RootElement.TryGetProperty("payload", out var payload))
                            AgentPalette.HandleTempSpecSelectionPayload(payload);
                        else if (action == "CLOSE_MODAL")
                            this.Close();
                    }
                    catch { /* 레거시 비-JSON 메시지 무시 */ }
                };
            }
            catch (Exception ex)
            {
                // CS0104 모호성 에러 방지를 위해 System.Windows 명시
                System.Windows.MessageBox.Show(
                    $"웹 화면을 불러오지 못했습니다:\n{ex.Message}",
                    "오류",
                    System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Error
                );
            }
        }
    }
}
