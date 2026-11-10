using ArrSubtitle.Cli;
using System.Text;

Console.OutputEncoding = new UTF8Encoding(false);
using var cancellation = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cancellation.Cancel(); };
return await CommandRunner.RunAsync(args, Console.Out, Console.Error, cancellation.Token);
