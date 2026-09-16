#pragma once

// Small platform boundary used by tone3000_client.cpp: HTTPS requests, PKCE
// crypto, opening the system browser, a one-shot local OAuth redirect
// listener, and small-secret storage. Same rule as platform.hpp /
// midi_transport.hpp: nothing in src/core may include a platform header
// (no <windows.h>, no Apple headers) -- extend this seam instead.
//
// Implementation:
//   src/platform/macos/net_client_mac.mm   (NSURLSession / CommonCrypto /
//                                            NSWorkspace / BSD sockets /
//                                            a private file under
//                                            Application Support for secret
//                                            storage -- see saveSecret below)
//   src/platform/windows/net_client_win.cpp (WinHTTP / BCrypt / Winsock /
//                                            ShellExecute / registry +
//                                            CryptProtectData)

#include <cstddef>
#include <cstdint>
#include <string>

namespace ntc::net {

struct HttpResponse {
    int status = 0;
    std::string body;
};

// Blocking HTTPS request to https://<host><path>. contentType/bearerToken
// may be empty to omit those headers. Returns false only on a
// transport-level failure (DNS, TLS, connect) with `error` set; a non-2xx
// HTTP status is still a successful call -- callers check
// HttpResponse::status themselves.
bool httpsRequest(const std::string& method, const std::string& host, const std::string& path,
                   const std::string& body, const std::string& contentType, const std::string& bearerToken,
                   HttpResponse& out, std::string& error);

// PKCE helpers (RFC 7636): sha256Base64Url is the S256 code_challenge
// transform; randomBase64Url generates the code_verifier/state values.
std::string sha256Base64Url(const std::string& text);
std::string randomBase64Url(std::size_t byteCount);

// Opens `url` in the user's default browser.
bool openUrlInSystemBrowser(const std::string& url);

// Listens on 127.0.0.1:port for exactly one HTTP request (the OAuth
// redirect), replies with a small confirmation page, and returns the
// request target (path+query) received in `requestTarget`. Blocks up to
// timeoutSeconds.
bool waitForLocalOAuthCallback(std::uint16_t port, int timeoutSeconds, std::string& requestTarget, std::string& error);

// Small named-secret store (a private file under Application Support on
// macOS, the registry + CryptProtectData on Windows -- see each platform's
// implementation for why Keychain/DPAPI-alternative tradeoffs were made).
// `name` is an opaque key (e.g. "tone3000.publishableKey",
// "tone3000.refreshToken"); no value is ever logged or echoed back in
// --json output by callers.
bool saveSecret(const std::string& name, const std::string& value);
bool loadSecret(const std::string& name, std::string& value);
bool deleteSecret(const std::string& name);

} // namespace ntc::net
