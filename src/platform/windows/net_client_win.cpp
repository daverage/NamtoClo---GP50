// Windows implementation of the src/core/net_client.hpp seam (see that
// header's comment). Ported from upstream NamToClo's original Windows-only
// tone3000_client.cpp (WinHTTP + BCrypt + Winsock + ShellExecute), split out
// behind the portable net_client.hpp interface so src/core/tone3000_client.cpp
// stays free of <windows.h> -- see CLAUDE.md's platform-boundary rule.
//
// saveSecret/loadSecret/deleteSecret use the same registry+CryptProtectData
// (DPAPI) storage upstream's gui.cpp used directly for the Tone3000 OAuth
// refresh token, generalized here into a small named-secret store under
// HKCU\Software\NamToClo\Secrets\<name> so any future caller can reuse it
// without re-deriving the registry/DPAPI plumbing.

#include "net_client.hpp"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <wincrypt.h>
#include <winhttp.h>
#include <bcrypt.h>
#include <shellapi.h>

#include <algorithm>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <vector>

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "crypt32.lib")

namespace ntc::net {
namespace {

constexpr wchar_t kUserAgent[] = L"NamToClo-Tone3000/1.0";
constexpr wchar_t kSecretsKeyPath[] = L"Software\\NamToClo\\Secrets";

std::wstring utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring out(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), n);
    return out;
}

std::string base64Url(const std::vector<unsigned char>& in) {
    if (in.empty()) return {};
    DWORD chars = 0;
    CryptBinaryToStringA(in.data(), static_cast<DWORD>(in.size()),
                         CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF, nullptr, &chars);
    std::string out(chars, '\0');
    CryptBinaryToStringA(in.data(), static_cast<DWORD>(in.size()),
                         CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF, out.data(), &chars);
    while (!out.empty() && (out.back() == '\0' || out.back() == '=')) out.pop_back();
    std::replace(out.begin(), out.end(), '+', '-');
    std::replace(out.begin(), out.end(), '/', '_');
    return out;
}

bool randomBytes(std::vector<unsigned char>& bytes) {
    return BCryptGenRandom(nullptr, bytes.data(), static_cast<ULONG>(bytes.size()),
                           BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0;
}

bool splitHostPath(const std::string& host, std::wstring& wideHost) {
    wideHost = utf8ToWide(host);
    return !wideHost.empty();
}

} // namespace

bool httpsRequest(const std::string& method, const std::string& host, const std::string& path,
                   const std::string& body, const std::string& contentType, const std::string& bearerToken,
                   HttpResponse& out, std::string& error) {
    std::wstring wideHost;
    if (!splitHostPath(host, wideHost)) { error = "Invalid host"; return false; }
    const std::wstring wideMethod = utf8ToWide(method);
    const std::wstring widePath = utf8ToWide(path);

    HINTERNET session = WinHttpOpen(kUserAgent, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, nullptr, nullptr, 0);
    if (!session) { error = "WinHttpOpen failed"; return false; }
    HINTERNET connect = WinHttpConnect(session, wideHost.c_str(), INTERNET_DEFAULT_HTTPS_PORT, 0);
    if (!connect) { WinHttpCloseHandle(session); error = "WinHttpConnect failed"; return false; }
    HINTERNET request = WinHttpOpenRequest(connect, wideMethod.c_str(), widePath.c_str(), nullptr,
                                           WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE);
    if (!request) {
        WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
        error = "WinHttpOpenRequest failed";
        return false;
    }
    std::wstring headers;
    if (!contentType.empty()) headers += L"Content-Type: " + utf8ToWide(contentType) + L"\r\n";
    if (!bearerToken.empty()) headers += L"Authorization: Bearer " + utf8ToWide(bearerToken) + L"\r\n";
    const BOOL sent = WinHttpSendRequest(request, headers.empty() ? WINHTTP_NO_ADDITIONAL_HEADERS : headers.c_str(),
                                         static_cast<DWORD>(headers.size()),
                                         body.empty() ? WINHTTP_NO_REQUEST_DATA : const_cast<char*>(body.data()),
                                         static_cast<DWORD>(body.size()), static_cast<DWORD>(body.size()), 0);
    if (!sent || !WinHttpReceiveResponse(request, nullptr)) {
        error = "HTTPS request failed (WinHTTP error " + std::to_string(GetLastError()) + ")";
        WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
        return false;
    }
    DWORD status = 0;
    DWORD statusSize = sizeof(status);
    WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &status, &statusSize, WINHTTP_NO_HEADER_INDEX);
    out.status = static_cast<int>(status);
    for (;;) {
        DWORD available = 0;
        if (!WinHttpQueryDataAvailable(request, &available) || available == 0) break;
        const std::size_t old = out.body.size();
        out.body.resize(old + available);
        DWORD read = 0;
        if (!WinHttpReadData(request, out.body.data() + old, available, &read)) break;
        out.body.resize(old + read);
    }
    WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
    return true;
}

std::string sha256Base64Url(const std::string& text) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD objectLen = 0, result = 0, hashLen = 0;
    std::vector<unsigned char> object;
    std::vector<unsigned char> digest;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0) return {};
    if (BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&objectLen), sizeof(objectLen), &result, 0) != 0 ||
        BCryptGetProperty(alg, BCRYPT_HASH_LENGTH, reinterpret_cast<PUCHAR>(&hashLen), sizeof(hashLen), &result, 0) != 0) {
        BCryptCloseAlgorithmProvider(alg, 0);
        return {};
    }
    object.resize(objectLen);
    digest.resize(hashLen);
    if (BCryptCreateHash(alg, &hash, object.data(), objectLen, nullptr, 0, 0) != 0 ||
        BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char*>(text.data())), static_cast<ULONG>(text.size()), 0) != 0 ||
        BCryptFinishHash(hash, digest.data(), hashLen, 0) != 0) {
        if (hash) BCryptDestroyHash(hash);
        BCryptCloseAlgorithmProvider(alg, 0);
        return {};
    }
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(alg, 0);
    return base64Url(digest);
}

std::string randomBase64Url(std::size_t byteCount) {
    std::vector<unsigned char> b(byteCount);
    return randomBytes(b) ? base64Url(b) : std::string{};
}

bool openUrlInSystemBrowser(const std::string& url) {
    const std::wstring wideUrl = utf8ToWide(url);
    return reinterpret_cast<INT_PTR>(ShellExecuteW(nullptr, L"open", wideUrl.c_str(), nullptr, nullptr, SW_SHOWNORMAL)) > 32;
}

bool waitForLocalOAuthCallback(std::uint16_t port, int timeoutSeconds, std::string& requestTarget, std::string& error) {
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { error = "WSAStartup failed"; return false; }
    SOCKET server = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (server == INVALID_SOCKET) { WSACleanup(); error = "Could not create OAuth callback socket"; return false; }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    BOOL exclusive = TRUE;
    setsockopt(server, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&exclusive), sizeof(exclusive));
    if (bind(server, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == SOCKET_ERROR || listen(server, 1) == SOCKET_ERROR) {
        closesocket(server);
        WSACleanup();
        error = "OAuth callback port " + std::to_string(port) + " is unavailable";
        return false;
    }
    fd_set set;
    FD_ZERO(&set);
    FD_SET(server, &set);
    timeval tv{ timeoutSeconds, 0 };
    if (select(0, &set, nullptr, nullptr, &tv) <= 0) {
        closesocket(server);
        WSACleanup();
        error = "OAuth authorization timed out";
        return false;
    }
    SOCKET client = accept(server, nullptr, nullptr);
    char buf[8192]{};
    const int n = recv(client, buf, static_cast<int>(sizeof(buf) - 1), 0);
    if (n > 0) {
        std::string req(buf, buf + n);
        const std::size_t a = req.find(' ');
        const std::size_t b = a == std::string::npos ? std::string::npos : req.find(' ', a + 1);
        if (a != std::string::npos && b != std::string::npos) requestTarget = req.substr(a + 1, b - a - 1);
    }
    const char reply[] =
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n"
        "<html><body style='font-family:Segoe UI'><h2>Tone3000 connected.</h2>"
        "<p>You can close this browser tab and return to NAM to CLO.</p></body></html>";
    send(client, reply, static_cast<int>(sizeof(reply) - 1), 0);
    closesocket(client);
    closesocket(server);
    WSACleanup();
    if (requestTarget.empty()) { error = "Invalid OAuth callback"; return false; }
    return true;
}

bool saveSecret(const std::string& name, const std::string& value) {
    if (value.empty()) return false;
    DATA_BLOB in{ static_cast<DWORD>(value.size()), reinterpret_cast<BYTE*>(const_cast<char*>(value.data())) };
    DATA_BLOB out{};
    if (!CryptProtectData(&in, L"NamToClo secret", nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out))
        return false;
    HKEY hKey = nullptr;
    const bool created = RegCreateKeyExW(HKEY_CURRENT_USER, kSecretsKeyPath, 0, nullptr, 0, KEY_SET_VALUE,
                                         nullptr, &hKey, nullptr) == ERROR_SUCCESS;
    bool ok = false;
    if (created) {
        const std::wstring wideName = utf8ToWide(name);
        ok = RegSetValueExW(hKey, wideName.c_str(), 0, REG_BINARY, out.pbData, out.cbData) == ERROR_SUCCESS;
        RegCloseKey(hKey);
    }
    LocalFree(out.pbData);
    return ok;
}

bool loadSecret(const std::string& name, std::string& value) {
    value.clear();
    HKEY hKey = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kSecretsKeyPath, 0, KEY_QUERY_VALUE, &hKey) != ERROR_SUCCESS) return false;
    const std::wstring wideName = utf8ToWide(name);
    DWORD type = 0, bytes = 0;
    if (RegQueryValueExW(hKey, wideName.c_str(), nullptr, &type, nullptr, &bytes) != ERROR_SUCCESS ||
        type != REG_BINARY || bytes == 0) {
        RegCloseKey(hKey);
        return false;
    }
    std::vector<BYTE> encrypted(bytes);
    const bool read = RegQueryValueExW(hKey, wideName.c_str(), nullptr, nullptr, encrypted.data(), &bytes) == ERROR_SUCCESS;
    RegCloseKey(hKey);
    if (!read) return false;

    DATA_BLOB in{ bytes, encrypted.data() };
    DATA_BLOB out{};
    if (!CryptUnprotectData(&in, nullptr, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) return false;
    value.assign(reinterpret_cast<const char*>(out.pbData), reinterpret_cast<const char*>(out.pbData) + out.cbData);
    LocalFree(out.pbData);
    return true;
}

bool deleteSecret(const std::string& name) {
    HKEY hKey = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kSecretsKeyPath, 0, KEY_SET_VALUE, &hKey) != ERROR_SUCCESS) return false;
    const std::wstring wideName = utf8ToWide(name);
    const bool ok = RegDeleteValueW(hKey, wideName.c_str()) == ERROR_SUCCESS;
    RegCloseKey(hKey);
    return ok;
}

} // namespace ntc::net
