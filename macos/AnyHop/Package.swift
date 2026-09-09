// swift-tools-version: 6.0
import PackageDescription

// Built with `swift build` under Command Line Tools — there is no full Xcode on
// the build host, so this is an SPM executable target rather than an Xcode
// project. swift-testing's macro plugin and its framework search paths are not
// on the default CLT search path, so the test target names them explicitly.
let developerDir = Context.environment["DEVELOPER_DIR"] ?? "/Library/Developer/CommandLineTools"
let developerFrameworks = "\(developerDir)/Library/Developer/Frameworks"
let testingMacros = "\(developerDir)/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib"
let testingInterop = "\(developerDir)/Library/Developer/usr/lib"

let package = Package(
    name: "AnyHop",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "AnyHop", targets: ["AnyHop"])
    ],
    targets: [
        .executableTarget(
            name: "AnyHop",
            path: "Sources/AnyHop",
            resources: [.copy("Resources")]
        ),
        .testTarget(
            name: "AnyHopTests",
            dependencies: ["AnyHop"],
            path: "Tests/AnyHopTests",
            swiftSettings: [
                .unsafeFlags([
                    "-F", developerFrameworks,
                    "-load-plugin-library", testingMacros,
                ])
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-F", developerFrameworks,
                    "-Xlinker", "-rpath",
                    "-Xlinker", developerFrameworks,
                    "-Xlinker", "-rpath",
                    "-Xlinker", testingInterop,
                ])
            ]
        ),
    ]
)
