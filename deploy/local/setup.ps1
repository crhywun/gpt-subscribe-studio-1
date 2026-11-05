$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$localRoot = Join-Path $projectRoot 'runtime/local'
foreach ($directory in @('config', 'data', 'caddy-data', 'caddy-config', 'certs', 'logs', 'node')) {
    New-Item -ItemType Directory -Path (Join-Path $localRoot $directory) -Force | Out-Null
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$authPath = Join-Path $localRoot 'config/container-auth.json'
if (-not (Test-Path -LiteralPath $authPath)) {
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 24
        $random.GetBytes($bytes)
        $password = [Convert]::ToBase64String($bytes)
    } finally {
        $random.Dispose()
    }
    [System.IO.File]::WriteAllText($authPath, (@{password=$password} | ConvertTo-Json), $utf8)
}
$proxyPath = Join-Path $localRoot 'data/proxy-gateways.json'
if (-not (Test-Path -LiteralPath $proxyPath)) {
    $disabled = @{ enabled=$false }
    $proxies = @{ version=1; checkout=$disabled; approve=$disabled }
    [System.IO.File]::WriteAllText($proxyPath, ($proxies | ConvertTo-Json -Depth 4), $utf8)
}
$envPath = Join-Path $projectRoot '.env.local'
if (-not (Test-Path -LiteralPath $envPath)) {
    $envText = @'
GSS_PUBLIC_ORIGIN=https://localhost:18443
GSS_USE_FAKE=false
GSS_ENABLE_PROMO=false
GSS_PLUS_ENGINE=har
'@
    [System.IO.File]::WriteAllText($envPath, $envText + "`n", $utf8)
}
$password = (Get-Content -Raw -LiteralPath $authPath | ConvertFrom-Json).password
$certPath = Join-Path $localRoot 'certs/localhost.crt'
$keyPath = Join-Path $localRoot 'certs/localhost.key'
if (-not (Test-Path -LiteralPath $certPath) -or -not (Test-Path -LiteralPath $keyPath)) {
    $rsa = [System.Security.Cryptography.RSA]::Create(2048)
    try {
        $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
            'CN=localhost', $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
            [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
        $san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
        $san.AddDnsName('localhost')
        $san.AddIpAddress([System.Net.IPAddress]::Loopback)
        $request.CertificateExtensions.Add($san.Build())
        $request.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
        $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5), [DateTimeOffset]::UtcNow.AddYears(1))
        try {
            [System.IO.File]::WriteAllText($certPath, $certificate.ExportCertificatePem(), $utf8)
            [System.IO.File]::WriteAllText($keyPath, $rsa.ExportPkcs8PrivateKeyPem(), $utf8)
        } finally {
            $certificate.Dispose()
        }
    } finally {
        $rsa.Dispose()
    }
}
$proxyConfig = Get-Content -Raw -LiteralPath $proxyPath | ConvertFrom-Json
$proxyStatus = if ($proxyConfig.checkout.enabled) { 'enabled; confirm the actual exit region in Settings before creating an order.' } else { 'disabled; configure your proxy in Settings before creating an order.' }
$accessText = "Local URL: https://localhost:18443`r`nLogin password: $password`r`nProxy: $proxyStatus`r`n"
[System.IO.File]::WriteAllText((Join-Path $localRoot 'ACCESS.txt'), $accessText, $utf8)
Write-Output 'Local configuration is ready. Login details are in runtime/local/ACCESS.txt.'
