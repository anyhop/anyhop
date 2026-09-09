import Foundation
import Testing

@testable import AnyHop

@Test func shellQuotingSurvivesEmbeddedQuotes() {
    #expect(shellQuoted("/Applications/AnyHop.app/x") == "'/Applications/AnyHop.app/x'")
    // A path containing a single quote must not be able to end the quoted run.
    #expect(shellQuoted("/a/it's/anyhop") == #"'/a/it'\''s/anyhop'"#)
}

@Test func appleScriptQuotingEscapesBackslashAndQuote() {
    #expect(appleScriptQuoted(#"say "hi""#) == #""say \"hi\"""#)
    #expect(appleScriptQuoted(#"a\b"#) == #""a\\b""#)
}

@Test func pathEncodingKeepsTheChannelSeparator() {
    // Channel ids are provider/name and that slash is a real path separator the
    // API routes on — only the pieces get escaped.
    #expect("protonvpn/nl 1".urlPathEncoded == "protonvpn/nl%201")
    #expect("a/b/c".urlPathEncoded == "a/b/c")
}

@Test func queryEncodingEscapesEverythingUnsafe() {
    #expect("a b&c".urlQueryEncoded == "a%20b%26c")
}

@Test func bundledExecutableIsNotResolvedFromPATH() {
    // With no app bundle around, there must be no fallback to a PATH `anyhop` —
    // that would drive a Homebrew/uv install's state directory.
    let core = CoreProcess(executable: nil, resourceURL: nil, environment: [:])
    #expect(!core.isAvailable)
}

@Test func processEnvironmentPinsTheAppIdentity() {
    let resources = URL(fileURLWithPath: "/Applications/AnyHop.app/Contents/Resources")
    let core = CoreProcess(
        executable: nil, resourceURL: resources, environment: ["PATH": "/usr/bin"])
    let env = core.processEnvironment()
    #expect(env["ANYHOP_SERVICE_OWNER"] == "macos-app")
    #expect(env["ANYHOP_SERVICE_PREFIX"] == resources.path)
    #expect(env["ANYHOP_HOME"]?.hasSuffix("Library/Application Support/AnyHop") == true)
}

@Test func explicitHomeIsNotOverridden() {
    let core = CoreProcess(
        executable: nil, resourceURL: URL(fileURLWithPath: "/tmp/R"),
        environment: ["ANYHOP_HOME": "/custom/home"])
    #expect(core.processEnvironment()["ANYHOP_HOME"] == "/custom/home")
}
