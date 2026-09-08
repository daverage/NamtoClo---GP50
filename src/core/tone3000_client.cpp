#include "tone3000_client.hpp"
#include "net_client.hpp"

#include <json.hpp>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <thread>

namespace ntc::tone3000 {
namespace {

constexpr char kHost[] = "www.tone3000.com";
constexpr std::uint16_t kCallbackPort = 17836;
constexpr char kRedirectUri[] = "http://127.0.0.1:17836/callback";

std::int64_t nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string jsonStringOrEmpty(const nlohmann::json& object, const char* key) {
    const auto it = object.find(key);
    if (it == object.end() || it->is_null()) return {};
    if (it->is_string()) return it->get<std::string>();
    return {};
}

std::string urlEncode(const std::string& s) {
    std::ostringstream o;
    o << std::uppercase << std::hex;
    for (unsigned char c : s) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') o << c;
        else o << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
    }
    return o.str();
}

std::string queryParam(const std::string& target, const std::string& key) {
    const auto q = target.find('?');
    if (q == std::string::npos) return {};
    const std::string needle = key + "=";
    std::size_t p = q + 1;
    while (p < target.size()) {
        std::size_t end = target.find('&', p);
        if (end == std::string::npos) end = target.size();
        if (target.compare(p, needle.size(), needle) == 0) {
            std::string v = target.substr(p + needle.size(), end - p - needle.size());
            std::string decoded;
            for (std::size_t i = 0; i < v.size(); ++i) {
                if (v[i] == '%' && i + 2 < v.size()) {
                    const std::string h = v.substr(i + 1, 2);
                    decoded.push_back(static_cast<char>(std::strtoul(h.c_str(), nullptr, 16)));
                    i += 2;
                } else {
                    decoded.push_back(v[i] == '+' ? ' ' : v[i]);
                }
            }
            return decoded;
        }
        p = end + 1;
    }
    return {};
}

// model_url is an absolute TONE3000 URL. The authenticated client must
// request only its path, matching the official reference client's
// `url.replace(/^https?:\/\/[^/]+/, '')` behaviour.
std::string apiPathFromModelUrl(const std::string& url) {
    const auto scheme = url.find("://");
    if (scheme != std::string::npos) {
        const auto path = url.find('/', scheme + 3);
        return path == std::string::npos ? std::string("/") : url.substr(path);
    }
    return url.empty() || url.front() == '/' ? url : "/" + url;
}

} // namespace

Client::Client(std::string publishableKey) : publishableKey_(std::move(publishableKey)) {}

void Client::setPublishableKey(std::string key) {
    publishableKey_ = std::move(key);
    if (publishableKey_.empty()) disconnect();
}

void Client::disconnect() noexcept {
    accessToken_.clear();
    refreshToken_.clear();
    accessTokenExpiresAtUnixMs_ = 0;
}

bool Client::restoreSession(std::string refreshTokenIn, std::string& error) {
    if (publishableKey_.rfind("t3k_pub_", 0) != 0) {
        error = "Tone3000 session has no valid publishable key";
        return false;
    }
    if (refreshTokenIn.empty()) {
        error = "No saved Tone3000 refresh token";
        return false;
    }
    disconnect();
    refreshToken_ = std::move(refreshTokenIn);
    return refresh(error);
}

bool Client::authenticateInteractive(std::string& error) {
    if (publishableKey_.rfind("t3k_pub_", 0) != 0) {
        error = "Enter a valid Tone3000 publishable key (t3k_pub_...)";
        return false;
    }
    const std::string verifier = net::randomBase64Url(32);
    const std::string state = net::randomBase64Url(16);
    const std::string challenge = net::sha256Base64Url(verifier);
    if (verifier.empty() || state.empty() || challenge.empty()) {
        error = "Could not generate PKCE values";
        return false;
    }
    const std::string url = "https://" + std::string(kHost) + "/api/v1/oauth/authorize?client_id=" +
        urlEncode(publishableKey_) + "&redirect_uri=" + urlEncode(kRedirectUri) +
        "&response_type=code&code_challenge=" + urlEncode(challenge) +
        "&code_challenge_method=S256&state=" + urlEncode(state) + "&format=nam";

    // Bind/listen must happen before the browser is opened, so start the
    // listener on its own thread first and open the browser once it is up.
    std::string target, listenerError;
    std::thread listener([&] { net::waitForLocalOAuthCallback(kCallbackPort, 180, target, listenerError); });
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
    if (!net::openUrlInSystemBrowser(url)) {
        listener.detach();
        error = "Could not open the system browser";
        return false;
    }
    listener.join();
    if (!listenerError.empty()) { error = listenerError; return false; }
    if (queryParam(target, "state") != state) { error = "OAuth state mismatch"; return false; }
    if (const auto oauthError = queryParam(target, "error"); !oauthError.empty()) {
        error = "Tone3000 OAuth error: " + oauthError;
        return false;
    }
    const std::string code = queryParam(target, "code");
    if (code.empty()) { error = "Tone3000 did not return an authorization code"; return false; }

    const std::string body = "grant_type=authorization_code&code=" + urlEncode(code) +
        "&code_verifier=" + urlEncode(verifier) + "&redirect_uri=" + urlEncode(kRedirectUri) +
        "&client_id=" + urlEncode(publishableKey_);
    net::HttpResponse r;
    if (!net::httpsRequest("POST", kHost, "/api/v1/oauth/token", body,
                           "application/x-www-form-urlencoded", {}, r, error)) return false;
    if (r.status < 200 || r.status >= 300) {
        error = "Token exchange failed: HTTP " + std::to_string(r.status) + " " + r.body;
        return false;
    }
    try {
        const auto j = nlohmann::json::parse(r.body);
        accessToken_ = j.value("access_token", "");
        refreshToken_ = j.value("refresh_token", "");
        accessTokenExpiresAtUnixMs_ = nowMs() + static_cast<std::int64_t>(j.value("expires_in", 3600)) * 1000;
    } catch (const std::exception& e) {
        error = std::string("Invalid token response: ") + e.what();
        return false;
    }
    if (accessToken_.empty()) { error = "Token response contained no access_token"; return false; }
    return true;
}

bool Client::refresh(std::string& error) {
    if (refreshToken_.empty()) { error = "No refresh token available"; return false; }
    const std::string body = "grant_type=refresh_token&refresh_token=" + urlEncode(refreshToken_) +
        "&client_id=" + urlEncode(publishableKey_);
    net::HttpResponse r;
    if (!net::httpsRequest("POST", kHost, "/api/v1/oauth/token", body,
                           "application/x-www-form-urlencoded", {}, r, error)) return false;
    if (r.status < 200 || r.status >= 300) {
        error = "Token refresh failed: HTTP " + std::to_string(r.status);
        disconnect();
        return false;
    }
    try {
        const auto j = nlohmann::json::parse(r.body);
        accessToken_ = j.value("access_token", "");
        refreshToken_ = j.value("refresh_token", refreshToken_);
        accessTokenExpiresAtUnixMs_ = nowMs() + static_cast<std::int64_t>(j.value("expires_in", 3600)) * 1000;
    } catch (const std::exception&) {
        error = "Invalid refresh response";
        return false;
    }
    return !accessToken_.empty();
}

bool Client::ensureAccessToken(std::string& error) {
    if (!connected()) { error = "Connect to Tone3000 first"; return false; }
    if (nowMs() > accessTokenExpiresAtUnixMs_ - 60000) return refresh(error);
    return true;
}

bool Client::searchNamTones(const std::string& query, int page, const std::string& sort, std::vector<Tone>& tones,
                            int& totalPages, int& totalResults, std::string& error) {
    if (!ensureAccessToken(error)) return false;
    if (page < 1) page = 1;
    const std::string path = "/api/v1/tones/search?format=nam&architecture=2&page_size=100&page=" +
        std::to_string(page) + "&sort=" + urlEncode(sort.empty() ? "best-match" : sort) + "&query=" + urlEncode(query);
    net::HttpResponse r;
    if (!net::httpsRequest("GET", kHost, path, {}, {}, accessToken_, r, error)) return false;
    if (r.status == 401 && refresh(error)) return searchNamTones(query, page, sort, tones, totalPages, totalResults, error);
    if (r.status < 200 || r.status >= 300) { error = "Tone search failed: HTTP " + std::to_string(r.status); return false; }
    try {
        const auto j = nlohmann::json::parse(r.body);
        tones.clear();
        totalPages = j.value("total_pages", 1);
        totalResults = j.value("total", 0);
        if (totalPages < 1) totalPages = 1;
        for (const auto& x : j.at("data")) {
            Tone t;
            t.id = x.value("id", 0LL);
            t.title = jsonStringOrEmpty(x, "title");
            t.gear = jsonStringOrEmpty(x, "gear");
            t.license = jsonStringOrEmpty(x, "license");
            t.modelsCount = x.value("models_count", 0);
            t.downloadsCount = x.value("downloads_count", 0);
            t.favoritesCount = x.value("favorites_count", 0);
            const auto userIt = x.find("user");
            if (userIt != x.end() && userIt->is_object()) t.creator = jsonStringOrEmpty(*userIt, "username");
            tones.push_back(std::move(t));
        }
    } catch (const std::exception& e) {
        error = std::string("Invalid search response: ") + e.what();
        return false;
    }
    return true;
}

bool Client::listModels(std::int64_t toneId, std::vector<Model>& models, std::string& error) {
    if (!ensureAccessToken(error)) return false;
    const std::string path = "/api/v1/models?tone_id=" + std::to_string(toneId) + "&architecture=2&page_size=100";
    net::HttpResponse r;
    if (!net::httpsRequest("GET", kHost, path, {}, {}, accessToken_, r, error)) return false;
    if (r.status < 200 || r.status >= 300) { error = "Model list failed: HTTP " + std::to_string(r.status); return false; }
    try {
        const auto j = nlohmann::json::parse(r.body);
        models.clear();
        for (const auto& x : j.at("data")) {
            Model m;
            m.id = x.value("id", 0LL);
            m.toneId = x.value("tone_id", 0LL);
            m.name = jsonStringOrEmpty(x, "name");
            m.size = jsonStringOrEmpty(x, "size");
            m.architectureVersion = jsonStringOrEmpty(x, "architecture_version");
            m.modelUrl = jsonStringOrEmpty(x, "model_url");
            if (m.modelUrl.size() >= 4 && m.modelUrl.substr(m.modelUrl.size() - 4) == ".nam") {
                models.push_back(std::move(m));
            }
        }
    } catch (const std::exception& e) {
        error = std::string("Invalid models response: ") + e.what();
        return false;
    }
    return true;
}

bool Client::downloadModel(const Model& model, const fs::path& destination, std::string& error) {
    if (!ensureAccessToken(error)) return false;
    net::HttpResponse r;
    if (!net::httpsRequest("GET", kHost, apiPathFromModelUrl(model.modelUrl), {}, {}, accessToken_, r, error)) return false;
    if (r.status < 200 || r.status >= 300) { error = "Model download failed: HTTP " + std::to_string(r.status); return false; }
    std::error_code ec;
    if (destination.has_parent_path()) fs::create_directories(destination.parent_path(), ec);
    std::ofstream f(destination, std::ios::binary);
    if (!f) { error = "Could not create downloaded NAM file"; return false; }
    f.write(r.body.data(), static_cast<std::streamsize>(r.body.size()));
    if (!f) { error = "Could not write downloaded NAM file"; return false; }
    return true;
}

} // namespace ntc::tone3000
