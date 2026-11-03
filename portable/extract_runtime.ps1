function Expand-WechatbotRuntimeArchive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Archive,
        [Parameter(Mandatory=$true)][string]$Destination,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedSha256,
        [Parameter(Mandatory=$true)][string[]]$RequiredFiles,
        [switch]$ForceVerify
    )
    if (-not ('WechatbotPortable.RuntimeArchiveExtractor' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -ReferencedAssemblies System.IO.Compression -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

namespace WechatbotPortable {
public static class RuntimeArchiveExtractor {
    const uint Invalid = 0xffffffff, Directory = 0x10, Reparse = 0x400;
    const string Marker = ".wechatbot-archive.sha256";
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateDirectoryW(string path, IntPtr security);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint GetFileAttributesW(string path);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool MoveFileExW(string source, string destination, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool DeleteFileW(string path);
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct FindData {
        public uint attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME creation, access, write;
        public uint sizeHigh, sizeLow, reserved0, reserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=14)] public string alternate;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr FindFirstFileW(string pattern, out FindData data);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool FindNextFileW(IntPtr handle, out FindData data);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool FindClose(IntPtr handle);

    sealed class AbsolutePath { public string root, value; public string[] parts; }
    sealed class Item { public ZipArchiveEntry entry; public string relative, path, hash; public bool directory; }

    static void NativeError(string operation, string path) {
        int error = Marshal.GetLastWin32Error();
        throw new Win32Exception(error, operation + ": " + path + " (" + new Win32Exception(error).Message + ")");
    }
    static void Segment(string value) {
        if (String.IsNullOrEmpty(value) || value.Length > 255 || value == "." || value == ".." || value.EndsWith(".") || value.EndsWith(" "))
            throw new InvalidDataException("Invalid archive/path component: " + value);
        foreach (char character in value)
            if (Char.IsControl(character) || ":*?<>|\"".IndexOf(character) >= 0)
                throw new InvalidDataException("Unsafe archive/path component: " + value);
        if (Regex.IsMatch(value, @"^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)
            || Regex.IsMatch(value, @"~[0-9]"))
            throw new InvalidDataException("Device or short-name alias component is not allowed: " + value);
    }
    static AbsolutePath Absolute(string input) {
        if (String.IsNullOrWhiteSpace(input)) throw new ArgumentException("Archive and destination must be absolute paths.");
        string value = input.Replace('/', '\\');
        if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) value = @"\\" + value.Substring(8);
        else if (value.StartsWith(@"\\?\", StringComparison.Ordinal)) value = value.Substring(4);
        string root, remaining;
        if (Regex.IsMatch(value, @"^[A-Za-z]:\\")) {
            root = @"\\?\" + value.Substring(0, 3);
            remaining = value.Substring(3);
        } else if (value.StartsWith(@"\\", StringComparison.Ordinal) && !value.StartsWith(@"\\.\", StringComparison.Ordinal)) {
            string[] components = value.Substring(2).Split('\\');
            if (components.Length < 3) throw new ArgumentException("UNC path must include server, share and a child.");
            Segment(components[0]); Segment(components[1]);
            root = @"\\?\UNC\" + components[0] + "\\" + components[1] + "\\";
            remaining = String.Join("\\", components, 2, components.Length - 2);
        } else throw new ArgumentException("Only absolute drive or UNC paths are supported.");
        remaining = remaining.TrimEnd('\\');
        string[] parts = remaining.Split('\\');
        foreach (string part in parts) Segment(part);
        string native = root + String.Join("\\", parts);
        if (native.Length > 32700) throw new PathTooLongException("Extended path exceeds the Windows native limit.");
        return new AbsolutePath { root=root, parts=parts, value=native };
    }
    static string Relative(string name, bool directory) {
        if (String.IsNullOrEmpty(name) || name[0] == '/' || name[0] == '\\')
            throw new InvalidDataException("Archive entry must be relative: " + name);
        string value = name.Replace('/', '\\');
        if (directory) value = value.Substring(0, value.Length - 1);
        string[] components = value.Split('\\');
        foreach (string component in components) Segment(component);
        if (String.Equals(components[0], Marker, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Archive cannot replace its completion marker.");
        return String.Join("\\", components);
    }
    static string Child(string destination, string relative) {
        string result = destination + "\\" + relative;
        if (result.Length > 32700) throw new PathTooLongException("Extended archive target exceeds the Windows native limit.");
        return result;
    }
    static uint? Attributes(string path) {
        uint attributes = GetFileAttributesW(path);
        if (attributes != Invalid) return attributes;
        int error = Marshal.GetLastWin32Error();
        if (error == 2 || error == 3) return null;
        NativeError("GetFileAttributesW failed", path); return null;
    }
    static void NoReparse(string path, uint? attributes) {
        if (attributes.HasValue && (attributes.Value & Reparse) != 0)
            throw new IOException("Reparse point is not allowed in runtime extraction: " + path);
    }
    static void Ancestors(AbsolutePath path) {
        string current = path.root;
        NoReparse(current, Attributes(current));
        foreach (string part in path.parts) {
            current = current.EndsWith("\\") ? current + part : current + "\\" + part;
            NoReparse(current, Attributes(current));
        }
    }
    static void InspectTree(string root) {
        uint? attributes = Attributes(root);
        NoReparse(root, attributes);
        if (!attributes.HasValue) return;
        if ((attributes.Value & Directory) == 0) throw new IOException("Runtime destination must be a directory: " + root);
        Stack<string> pending = new Stack<string>(); pending.Push(root);
        while (pending.Count > 0) {
            string parent = pending.Pop(); FindData data;
            IntPtr handle = FindFirstFileW(parent + "\\*", out data);
            if (handle == new IntPtr(-1)) {
                int error = Marshal.GetLastWin32Error();
                if (error == 2 || error == 18) continue;
                NativeError("FindFirstFileW failed", parent);
            }
            try {
                while (true) {
                    if (data.name != "." && data.name != "..") {
                        string child = Child(parent, data.name);
                        NoReparse(child, data.attributes);
                        if ((data.attributes & Directory) != 0) pending.Push(child);
                    }
                    if (!FindNextFileW(handle, out data)) {
                        if (Marshal.GetLastWin32Error() != 18) NativeError("FindNextFileW failed", parent);
                        break;
                    }
                }
            } finally { FindClose(handle); }
        }
    }
    static FileStream Open(string path, bool write) {
        SafeFileHandle handle = CreateFileW(path, write ? 0x40000000u : 0x80000000u, 1, IntPtr.Zero,
            write ? 1u : 3u, 0x00200000u | (write ? 0x80000000u : 0x80u), IntPtr.Zero);
        if (handle.IsInvalid) {
            int error=Marshal.GetLastWin32Error(); handle.Dispose();
            throw new Win32Exception(error, "CreateFileW failed: " + path + " (" + new Win32Exception(error).Message + ")");
        }
        try { return new FileStream(handle, write ? FileAccess.Write : FileAccess.Read, 65536, false); }
        catch { handle.Dispose(); throw; }
    }
    static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
    static string Hash(Stream stream) { using (SHA256 sha = SHA256.Create()) return Hex(sha.ComputeHash(stream)); }
    static string EntryHash(Item item) {
        if (item.hash != null) return item.hash;
        using (Stream stream = item.entry.Open()) {
            using (SHA256 sha = SHA256.Create()) {
                byte[] buffer = new byte[65536]; long length = 0; int read;
                while ((read = stream.Read(buffer, 0, buffer.Length)) > 0) {
                    length += read;
                    if (length > item.entry.Length) throw new InvalidDataException("Archive entry exceeds its declared length: " + item.relative);
                    sha.TransformBlock(buffer, 0, read, null, 0);
                }
                if (length != item.entry.Length) throw new InvalidDataException("Archive entry is incomplete: " + item.relative);
                sha.TransformFinalBlock(new byte[0], 0, 0); item.hash = Hex(sha.Hash);
            }
        }
        return item.hash;
    }
    static bool Matches(Item item) {
        uint? attributes = Attributes(item.path); NoReparse(item.path, attributes);
        if (!attributes.HasValue) return false;
        if ((attributes.Value & Directory) != 0) throw new IOException("Archive file target is a directory: " + item.path);
        using (FileStream file = Open(item.path, false))
            return file.Length == item.entry.Length && String.Equals(Hash(file), EntryHash(item), StringComparison.Ordinal);
    }
    static void EnsureDirectory(string path) {
        uint? attributes = Attributes(path); NoReparse(path, attributes);
        if (attributes.HasValue) {
            if ((attributes.Value & Directory) == 0) throw new IOException("Archive directory target is a file: " + path);
            return;
        }
        if (!CreateDirectoryW(path, IntPtr.Zero)) {
            if (Marshal.GetLastWin32Error() != 183) NativeError("CreateDirectoryW failed", path);
            attributes = Attributes(path); NoReparse(path, attributes);
            if (!attributes.HasValue || (attributes.Value & Directory) == 0) throw new IOException("Runtime directory could not be created: " + path);
        }
    }
    static void Parents(string destination, string relative) {
        string[] components = relative.Split('\\'); string current = destination;
        for (int index=0; index<components.Length-1; index++) { current=Child(current, components[index]); EnsureDirectory(current); }
    }
    static void AtomicWrite(string path, Stream input, long expectedLength, string expectedHash) {
        int split = path.LastIndexOf('\\');
        string temporary = path.Substring(0, split+1) + ".wechatbot-tmp-" + Guid.NewGuid().ToString("N");
        bool created = false;
        try {
            using (FileStream output = Open(temporary, true)) {
                created = true;
                using (SHA256 sha = SHA256.Create()) {
                    byte[] buffer = new byte[65536]; long length = 0; int read;
                    while ((read = input.Read(buffer, 0, buffer.Length)) > 0) {
                        length += read;
                        if (length > expectedLength) throw new InvalidDataException("Runtime entry exceeds declared byte length: " + path);
                        output.Write(buffer, 0, read); sha.TransformBlock(buffer, 0, read, null, 0);
                    }
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    if (length != expectedLength || !String.Equals(Hex(sha.Hash), expectedHash, StringComparison.Ordinal))
                        throw new InvalidDataException("Runtime entry byte length or SHA256 mismatch: " + path);
                    output.Flush(true);
                }
            }
            NoReparse(path, Attributes(path));
            if (!MoveFileExW(temporary, path, 1u | 8u)) NativeError("MoveFileExW failed", path);
            created = false;
        } finally { if (created) DeleteFileW(temporary); }
    }
    static bool MarkerMatches(string path, string expected) {
        uint? attributes=Attributes(path); NoReparse(path, attributes);
        if (!attributes.HasValue || (attributes.Value & Directory) != 0) return false;
        using (FileStream stream=Open(path, false)) {
            if (stream.Length > 256) return false;
            using (StreamReader reader=new StreamReader(stream, new UTF8Encoding(false, true))) {
                try { return String.Equals(reader.ReadToEnd().Trim(), expected, StringComparison.OrdinalIgnoreCase); }
                catch (DecoderFallbackException) { return false; }
            }
        }
    }
    static void Node(Dictionary<string, bool> nodes, Dictionary<string, string> spellings, string name, bool directory) {
        bool previous; string spelling;
        if (nodes.TryGetValue(name, out previous) && previous != directory)
            throw new InvalidDataException("Archive file/directory ancestry conflict: " + name);
        if (spellings.TryGetValue(name, out spelling) && spelling != name)
            throw new InvalidDataException("Archive paths collide by case: " + name);
        nodes[name]=directory; spellings[name]=name;
    }
    public static string Extract(string archive, string destination, string expectedSha256, string[] requiredFiles, bool forceVerify) {
        if (!Regex.IsMatch(expectedSha256 ?? "", "^[0-9a-fA-F]{64}$")) throw new ArgumentException("Expected SHA256 must contain 64 hexadecimal digits.");
        if (requiredFiles == null || requiredFiles.Length == 0) throw new ArgumentException("At least one required runtime file must be supplied.");
        string expected=expectedSha256.ToLowerInvariant(); AbsolutePath archivePath=Absolute(archive), root=Absolute(destination);
        Ancestors(archivePath); Ancestors(root); InspectTree(root.value);
        using (FileStream source=Open(archivePath.value, false)) {
            if (!String.Equals(Hash(source), expected, StringComparison.Ordinal)) throw new InvalidDataException("Runtime archive SHA256 does not match the verified manifest.");
            source.Position=0;
            using (ZipArchive zip=new ZipArchive(source, ZipArchiveMode.Read, true)) {
                List<Item> items=new List<Item>();
                HashSet<string> names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                Dictionary<string, bool> nodes=new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
                Dictionary<string, string> spellings=new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                Dictionary<string, Item> files=new Dictionary<string, Item>(StringComparer.OrdinalIgnoreCase);
                long totalLength=0;
                foreach (ZipArchiveEntry entry in zip.Entries) {
                    if (items.Count >= 200000 || entry.Length < 0 || entry.Length > 4L*1024*1024*1024)
                        throw new InvalidDataException("Runtime archive exceeds its entry count or file byte limit.");
                    totalLength=checked(totalLength+entry.Length);
                    if (totalLength > 8L*1024*1024*1024) throw new InvalidDataException("Runtime archive exceeds its total byte limit.");
                    bool directory=entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\");
                    string relative=Relative(entry.FullName, directory);
                    int unixType=(entry.ExternalAttributes >> 16) & 0xf000;
                    if ((unixType != 0 && unixType != 0x8000 && unixType != 0x4000) || (entry.ExternalAttributes & (int)Reparse) != 0
                        || unixType == 0x4000 && !directory || unixType == 0x8000 && directory)
                        throw new InvalidDataException("Archive links or special entries are not allowed: " + relative);
                    if (!names.Add(relative)) throw new InvalidDataException("Duplicate archive target: " + relative);
                    string[] components=relative.Split('\\'); string parent="";
                    for (int index=0; index<components.Length-1; index++) {
                        parent=parent.Length == 0 ? components[index] : parent+"\\"+components[index]; Node(nodes, spellings, parent, true);
                    }
                    Node(nodes, spellings, relative, directory);
                    Item item=new Item { entry=entry, relative=relative, path=Child(root.value, relative), directory=directory };
                    if (String.Equals(item.path, archivePath.value, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Archive cannot replace its own source file.");
                    if (directory && entry.Length != 0) throw new InvalidDataException("Archive directory contains file data: " + relative);
                    if (!directory) files.Add(relative, item);
                    items.Add(item);
                }
                if (items.Count == 0) throw new InvalidDataException("Runtime archive is empty.");
                foreach (KeyValuePair<string, bool> node in nodes) {
                    string target=Child(root.value, node.Key); uint? attributes=Attributes(target); NoReparse(target, attributes);
                    if (attributes.HasValue && ((attributes.Value & Directory) != 0) != node.Value)
                        throw new IOException("Existing runtime target has a file/directory conflict: " + target);
                }
                List<Item> required=new List<Item>();
                foreach (string name in requiredFiles) {
                    string relative=Relative(name, false); Item item;
                    if (!files.TryGetValue(relative, out item)) throw new InvalidDataException("Required runtime file is missing from archive: " + relative);
                    required.Add(item);
                }
                string marker=Child(root.value, Marker);
                uint? markerAttributes=Attributes(marker); NoReparse(marker, markerAttributes);
                if (markerAttributes.HasValue && (markerAttributes.Value & Directory) != 0)
                    throw new IOException("Runtime completion marker is a directory: " + marker);
                bool reusable=!forceVerify && MarkerMatches(marker, expected);
                if (reusable) foreach (Item item in required) if (!Matches(item)) { reusable=false; break; }
                if (reusable) return "Runtime archive reused; required files verified (" + required.Count + ").";
                EnsureDirectory(root.value);
                // An earlier success marker cannot certify an interrupted repair.
                // This is our own metadata file, never user configuration.
                if (markerAttributes.HasValue && !DeleteFileW(marker)) NativeError("Cannot invalidate runtime completion marker", marker);
                int repaired=0, unchanged=0;
                foreach (Item item in items) {
                    Parents(root.value, item.relative);
                    if (item.directory) { EnsureDirectory(item.path); continue; }
                    if (Matches(item)) { unchanged++; continue; }
                    string hash=EntryHash(item);
                    using (Stream input=item.entry.Open()) AtomicWrite(item.path, input, item.entry.Length, hash);
                    repaired++;
                }
                foreach (Item item in required) if (!Matches(item)) throw new InvalidDataException("Required runtime verification failed: " + item.relative);
                byte[] markerBytes=Encoding.UTF8.GetBytes(expected+"\r\n");
                using (MemoryStream input=new MemoryStream(markerBytes)) {
                    string hash=Hash(input); input.Position=0; AtomicWrite(marker, input, markerBytes.Length, hash);
                }
                return "Runtime archive verified; repaired " + repaired + ", unchanged " + unchanged + ".";
            }
        }
    }
}
}
'@
    }
    try {
        [WechatbotPortable.RuntimeArchiveExtractor]::Extract($Archive, $Destination, $ExpectedSha256, $RequiredFiles, $ForceVerify.IsPresent)
    } catch {
        $taskOriginalError = $_.Exception
        while ($taskOriginalError.InnerException) { $taskOriginalError = $taskOriginalError.InnerException }
        throw $taskOriginalError
    }
}
