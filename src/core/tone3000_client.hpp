#pragma once

// Portable Tone3000.com client: OAuth (PKCE) login, NAM tone search, model
// listing, and model download. Ported from upstream's Windows-only
// (WinHTTP/BCrypt/registry) implementation to the cross-platform
// src/core/net_client.hpp seam -- see CLAUDE.md and the tone3000
// integration plan for context. All HTTP/crypto/browser/secret-storage
// calls go through net_client.hpp; this file has no platform dependency.

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace ntc::tone3000 {

namespace fs = std::filesystem;

struct Tone {
    std::int64_t id = 0;
    std::string title;
    std::string creator;
    std::string gear;
    std::string license;
    int modelsCount = 0;
    int downloadsCount = 0;
    int favoritesCount = 0;
};

struct Model {
    std::int64_t id = 0;
    std::int64_t toneId = 0;
    std::string name;
    std::string size;
    std::string architectureVersion;
    std::string modelUrl;
};

class Client {
public:
    explicit Client(std::string publishableKey = {});

    void setPublishableKey(std::string key);
    const std::string& publishableKey() const noexcept { return publishableKey_; }

    bool connected() const noexcept { return !accessToken_.empty(); }
    void disconnect() noexcept;

    // Restores a previous OAuth session from a persisted refresh token
    // (see net_client.hpp's saveSecret/loadSecret; the CLI is the caller
    // that decides when to persist/restore).
    bool restoreSession(std::string refreshToken, std::string& error);

    // Opens the system browser, receives the OAuth callback on
    // 127.0.0.1:17836, verifies PKCE state, and exchanges the
    // authorization code for bearer tokens.
    bool authenticateInteractive(std::string& error);

    // Valid only after a successful authenticateInteractive/restoreSession
    // call -- callers persist this to reconnect on a later run.
    const std::string& refreshToken() const noexcept { return refreshToken_; }

    bool searchNamTones(const std::string& query, int page, const std::string& sort, std::vector<Tone>& tones,
                        int& totalPages, int& totalResults, std::string& error);
    bool listModels(std::int64_t toneId, std::vector<Model>& models, std::string& error);
    bool downloadModel(const Model& model, const fs::path& destination, std::string& error);

private:
    bool ensureAccessToken(std::string& error);
    bool refresh(std::string& error);

    std::string publishableKey_;
    std::string accessToken_;
    std::string refreshToken_;
    std::int64_t accessTokenExpiresAtUnixMs_ = 0;
};

} // namespace ntc::tone3000
