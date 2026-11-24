// Downloads originals from iCloud Photos with Apple's PhotoKit, for items the Photos
// "export" AppleScript command leaves empty (it doesn't download items that are only in iCloud).
//
//   photokit_export <item id> <folder> [<item id> <folder> ...]
//
// The item ids are the ones Photos' AppleScript uses (they are PhotoKit local identifiers).
// Prints one line per item: "OK<tab>id" or "FAIL<tab>id<tab>reason". Only reads the library.
// icloud_albums.py compiles this once with swiftc (part of Xcode Command Line Tools).
import Foundation
import Photos

setbuf(stdout, nil)

func authorize() -> PHAuthorizationStatus {
    var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    if status == .notDetermined {
        let done = DispatchSemaphore(value: 0)
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { s in
            status = s
            done.signal()
        }
        done.wait()
    }
    return status
}

let status = authorize()
guard status == .authorized || status == .limited else {
    print("NOACCESS")
    exit(2)
}

let args = Array(CommandLine.arguments.dropFirst())
var i = 0
while i + 1 < args.count {
    let id = args[i], folder = args[i + 1]
    i += 2
    guard let asset = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else {
        print("FAIL\t\(id)\tnot found in the library")
        continue
    }
    let resources = PHAssetResource.assetResources(for: asset)
    let wanted: [PHAssetResourceType] = asset.mediaType == .video ? [.video, .fullSizeVideo] : [.photo, .fullSizePhoto]
    var pick: PHAssetResource? = nil
    for type in wanted {
        if let r = resources.first(where: { $0.type == type }) {
            pick = r
            break
        }
    }
    guard let resource = pick ?? resources.first else {
        print("FAIL\t\(id)\tno file to download")
        continue
    }
    let url = URL(fileURLWithPath: folder).appendingPathComponent(resource.originalFilename)
    try? FileManager.default.removeItem(at: url)
    let options = PHAssetResourceRequestOptions()
    options.isNetworkAccessAllowed = true  // download it from iCloud
    let done = DispatchSemaphore(value: 0)
    var failure: Error? = nil
    PHAssetResourceManager.default().writeData(for: resource, toFile: url, options: options) { error in
        failure = error
        done.signal()
    }
    if done.wait(timeout: .now() + 1200) == .timedOut {
        print("FAIL\t\(id)\ttook more than 20 minutes to download from iCloud")
    } else if let e = failure {
        print("FAIL\t\(id)\t\(e.localizedDescription.replacingOccurrences(of: "\n", with: " "))")
    } else {
        print("OK\t\(id)")
    }
}
