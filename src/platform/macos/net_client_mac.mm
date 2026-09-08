#include "net_client.hpp"

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <cstdint>
#include <vector>

// macOS implementation of the src/core/net_client.hpp seam used by
// tone3000_client.cpp -- see the tone3000 integration plan. Uses only
// system frameworks (Foundation/NSURLSession, Security.framework Keychain
// and SecRandomCopyBytes, CommonCrypto) plus BSD sockets for the local
// OAuth callback listener; no new third-party dependency.

namespace ntc::net {
namespace {

constexpr const char* kKeychainService = "com.namtoclo.tone3000";

std::string base64UrlFromData(NSData* data) {
    NSString* b64 = [data base64EncodedStringWithOptions:0];
    std::string out = b64.UTF8String ? b64.UTF8String : "";
    std::replace(out.begin(), out.end(), '+', '-');
    std::replace(out.begin(), out.end(), '/', '_');
    while (!out.empty() && out.back() == '=') out.pop_back();
    return out;
}

} // namespace

bool httpsRequest(const std::string& method, const std::string& host, const std::string& path,
                   const std::string& body, const std::string& contentType, const std::string& bearerToken,
                   HttpResponse& out, std::string& error) {
    NSString* urlString = [NSString stringWithFormat:@"https://%s%s", host.c_str(), path.c_str()];
    NSURL* url = [NSURL URLWithString:urlString];
    if (!url) { error = "Invalid request URL"; return false; }

    NSMutableURLRequest* request = [NSMutableURLRequest requestWithURL:url];
    request.HTTPMethod = [NSString stringWithUTF8String:method.c_str()];
    if (!contentType.empty()) {
        [request setValue:[NSString stringWithUTF8String:contentType.c_str()] forHTTPHeaderField:@"Content-Type"];
    }
    if (!bearerToken.empty()) {
        NSString* bearer = [NSString stringWithFormat:@"Bearer %s", bearerToken.c_str()];
        [request setValue:bearer forHTTPHeaderField:@"Authorization"];
    }
    if (!body.empty()) {
        request.HTTPBody = [NSData dataWithBytes:body.data() length:body.size()];
    }

    __block NSInteger statusCode = 0;
    __block NSData* responseData = nil;
    __block NSString* errorString = nil;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);

    NSURLSessionConfiguration* config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    NSURLSession* session = [NSURLSession sessionWithConfiguration:config];
    NSURLSessionDataTask* task = [session
        dataTaskWithRequest:request
          completionHandler:^(NSData* data, NSURLResponse* response, NSError* taskError) {
              if (taskError) {
                  errorString = taskError.localizedDescription;
              } else {
                  statusCode = [(NSHTTPURLResponse*)response statusCode];
                  responseData = data;
              }
              dispatch_semaphore_signal(sem);
          }];
    [task resume];
    dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);

    if (errorString) {
        error = std::string("HTTPS request failed: ") + errorString.UTF8String;
        return false;
    }
    out.status = static_cast<int>(statusCode);
    if (responseData) {
        out.body.assign(static_cast<const char*>(responseData.bytes), responseData.length);
    }
    return true;
}

std::string sha256Base64Url(const std::string& text) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(text.data(), static_cast<CC_LONG>(text.size()), digest);
    NSData* data = [NSData dataWithBytes:digest length:sizeof(digest)];
    return base64UrlFromData(data);
}

std::string randomBase64Url(std::size_t byteCount) {
    std::vector<std::uint8_t> bytes(byteCount);
    if (SecRandomCopyBytes(kSecRandomDefault, bytes.size(), bytes.data()) != errSecSuccess) return {};
    NSData* data = [NSData dataWithBytes:bytes.data() length:bytes.size()];
    return base64UrlFromData(data);
}

bool openUrlInSystemBrowser(const std::string& url) {
    NSURL* nsUrl = [NSURL URLWithString:[NSString stringWithUTF8String:url.c_str()]];
    if (!nsUrl) return false;
    return [[NSWorkspace sharedWorkspace] openURL:nsUrl] == YES;
}

bool waitForLocalOAuthCallback(std::uint16_t port, int timeoutSeconds, std::string& requestTarget, std::string& error) {
    const int server = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (server < 0) { error = std::string("Could not create OAuth callback socket: ") + std::strerror(errno); return false; }

    int reuse = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    if (bind(server, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0 || listen(server, 1) < 0) {
        close(server);
        error = "Port " + std::to_string(port) + " is unavailable for the Tone3000 OAuth callback";
        return false;
    }

    fd_set set;
    FD_ZERO(&set);
    FD_SET(server, &set);
    timeval tv{timeoutSeconds, 0};
    const int selected = select(server + 1, &set, nullptr, nullptr, &tv);
    if (selected <= 0) {
        close(server);
        error = "Tone3000 authorization timed out";
        return false;
    }

    const int client = accept(server, nullptr, nullptr);
    if (client < 0) {
        close(server);
        error = "Could not accept the OAuth callback connection";
        return false;
    }

    char buf[8192];
    const ssize_t n = recv(client, buf, sizeof(buf) - 1, 0);
    if (n > 0) {
        buf[n] = '\0';
        const std::string req(buf, static_cast<std::size_t>(n));
        const auto a = req.find(' ');
        const auto b = a == std::string::npos ? std::string::npos : req.find(' ', a + 1);
        if (a != std::string::npos && b != std::string::npos) requestTarget = req.substr(a + 1, b - a - 1);
    }

    static const char kReply[] =
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n"
        "<html><body style='font-family:-apple-system'><h2>Tone3000 connected.</h2>"
        "<p>You can close this browser tab and return to NAM to CLO.</p></body></html>";
    send(client, kReply, sizeof(kReply) - 1, 0);
    close(client);
    close(server);

    if (requestTarget.empty()) { error = "Invalid OAuth callback"; return false; }
    return true;
}

bool saveSecret(const std::string& name, const std::string& value) {
    NSString* service = [NSString stringWithUTF8String:kKeychainService];
    NSString* account = [NSString stringWithUTF8String:name.c_str()];
    NSData* data = [NSData dataWithBytes:value.data() length:value.size()];

    NSDictionary* query = @{
        (__bridge id)kSecClass : (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService : service,
        (__bridge id)kSecAttrAccount : account,
    };
    SecItemDelete((__bridge CFDictionaryRef)query);

    NSMutableDictionary* item = [query mutableCopy];
    item[(__bridge id)kSecValueData] = data;
    item[(__bridge id)kSecAttrAccessible] = (__bridge id)kSecAttrAccessibleAfterFirstUnlock;
    return SecItemAdd((__bridge CFDictionaryRef)item, nullptr) == errSecSuccess;
}

bool loadSecret(const std::string& name, std::string& value) {
    NSString* service = [NSString stringWithUTF8String:kKeychainService];
    NSString* account = [NSString stringWithUTF8String:name.c_str()];
    NSDictionary* query = @{
        (__bridge id)kSecClass : (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService : service,
        (__bridge id)kSecAttrAccount : account,
        (__bridge id)kSecReturnData : @YES,
        (__bridge id)kSecMatchLimit : (__bridge id)kSecMatchLimitOne,
    };
    CFTypeRef result = nullptr;
    if (SecItemCopyMatching((__bridge CFDictionaryRef)query, &result) != errSecSuccess || !result) return false;
    NSData* data = (__bridge_transfer NSData*)result;
    value.assign(static_cast<const char*>(data.bytes), data.length);
    return true;
}

bool deleteSecret(const std::string& name) {
    NSString* service = [NSString stringWithUTF8String:kKeychainService];
    NSString* account = [NSString stringWithUTF8String:name.c_str()];
    NSDictionary* query = @{
        (__bridge id)kSecClass : (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService : service,
        (__bridge id)kSecAttrAccount : account,
    };
    const OSStatus status = SecItemDelete((__bridge CFDictionaryRef)query);
    return status == errSecSuccess || status == errSecItemNotFound;
}

} // namespace ntc::net
