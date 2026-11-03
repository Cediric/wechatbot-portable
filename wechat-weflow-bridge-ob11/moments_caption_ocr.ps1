param(
    [Parameter(Mandatory = $true)]
    [string] $ImagePath
)

# No desktop access or network: this process opens only the supplied PNG.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
[Console]::InputEncoding = $utf8
$OutputEncoding = $utf8
$stream = $null
$bitmap = $null
$converted = $null

try {
    if (-not [System.IO.Path]::IsPathRooted($ImagePath)) { throw 'An absolute PNG path is required.' }
    $fileInfo = New-Object System.IO.FileInfo($ImagePath)
    if (-not $fileInfo.Exists -or $fileInfo.Length -le 0 -or $fileInfo.Length -gt 8388608) {
        throw 'PNG file bounds are invalid.'
    }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    if ($null -eq $asTask) { throw 'WinRT await support is unavailable.' }
    function Await-Result($Operation, [Type] $ResultType) {
        $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        $task.GetAwaiter().GetResult()
    }

    $language = New-Object Windows.Globalization.Language('zh-Hans-CN')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) { throw 'The local Chinese OCR language is unavailable.' }
    $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($fileInfo.FullName)) ([Windows.Storage.StorageFile])
    $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    if ($decoder.PixelWidth -le 0 -or $decoder.PixelHeight -le 0 -or
        $decoder.PixelWidth -gt 4096 -or $decoder.PixelHeight -gt 4096 -or
        ([long] $decoder.PixelWidth * [long] $decoder.PixelHeight) -gt 4194304 -or
        $decoder.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
        $decoder.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
        throw 'Decoded PNG dimensions exceed the OCR bounds.'
    }
    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $converted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert(
        $bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
    $result = Await-Result ($engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
    $text = $result.Text
    if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 8192 -or $text.IndexOf([char] 0) -ge 0) {
        throw 'OCR did not return a bounded caption.'
    }
    # One JSON string, never profile messages, paths, pixels or diagnostic text.
    [Console]::Out.Write(($text | ConvertTo-Json -Compress))
} catch {
    [Console]::Error.Write('Windows caption OCR failed.')
    exit 1
} finally {
    if ($null -ne $converted) { $converted.Dispose() }
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
