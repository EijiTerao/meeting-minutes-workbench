using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

// Explicit WASAPI shared-mode render-loopback capture. No microphone API is used.
public static class WorkbenchSystemAudio
{
    const int ERender = 0, EConsole = 0, DeviceStateActive = 1;
    const uint Loopback = 0x00020000, BufferDiscontinuity = 0x1, BufferSilent = 0x2;
    const int ShareModeShared = 0;
    static readonly Guid EnumeratorClsid = new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E");
    static readonly Guid IidEnumerator = new Guid("A95664D2-9614-4F35-A746-DE8DB63617E6");
    static readonly Guid IidAudioClient = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
    static readonly Guid IidCaptureClient = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
    static readonly Guid PcmSubtype = new Guid("00000001-0000-0010-8000-00aa00389b71");
    static readonly Guid FloatSubtype = new Guid("00000003-0000-0010-8000-00aa00389b71");
    static readonly Guid IidPropertyStore = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    static readonly Guid DeviceInterfaceGuid = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0");

    [StructLayout(LayoutKind.Sequential)]
    struct PropertyKey { public Guid FormatId; public uint PropertyId; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    struct PropVariant { [FieldOffset(0)] public ushort Type; [FieldOffset(8)] public IntPtr Value; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }
    [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant value);

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(int flow, int mask, out IMMDeviceCollection devices);
        [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice device);
        [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
        [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr callback);
        [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr callback);
    }
    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection
    {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int Item(uint index, out IMMDevice device);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice
    {
        [PreserveSig] int Activate(ref Guid iid, uint context, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object instance);
        [PreserveSig] int OpenPropertyStore(int access, out IntPtr store);
        [PreserveSig] int GetId(out IntPtr id);
        [PreserveSig] int GetState(out uint state);
    }
    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, uint flags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
        [PreserveSig] int GetBufferSize(out uint frames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint frames);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr handle);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }
    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig] int ReleaseBuffer(uint frames);
        [PreserveSig] int GetNextPacketSize(out uint frames);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    struct WaveFormatEx
    {
        public ushort FormatTag, Channels;
        public uint SamplesPerSec, AvgBytesPerSec;
        public ushort BlockAlign, BitsPerSample, ExtraSize;
    }

    static void Check(int hr, string operation)
    {
        if (hr < 0) Marshal.ThrowExceptionForHR(hr, new IntPtr(-1));
    }

    static IMMDeviceEnumerator Enumerator()
    {
        return (IMMDeviceEnumerator)Activator.CreateInstance(Type.GetTypeFromCLSID(EnumeratorClsid, true));
    }

    static string ReadId(IMMDevice device)
    {
        IntPtr raw;
        Check(device.GetId(out raw), "IMMDevice.GetId");
        try { return Marshal.PtrToStringUni(raw); }
        finally { Marshal.FreeCoTaskMem(raw); }
    }

    static string ReadFriendlyName(IMMDevice device)
    {
        IntPtr rawStore;
        Check(device.OpenPropertyStore(0, out rawStore), "Open endpoint properties");
        IPropertyStore store = (IPropertyStore)Marshal.GetObjectForIUnknown(rawStore);
        Marshal.Release(rawStore);
        var key = new PropertyKey { FormatId = DeviceInterfaceGuid, PropertyId = 14 };
        PropVariant value = new PropVariant();
        int hr = store.GetValue(ref key, out value);
        try
        {
            Check(hr, "Read endpoint friendly name");
            return value.Type == 31 && value.Value != IntPtr.Zero ? Marshal.PtrToStringUni(value.Value) : null;
        }
        finally { PropVariantClear(ref value); }
    }

    public static string[] ListEndpoints()
    {
        var result = new List<string>();
        IMMDeviceCollection devices;
        Check(Enumerator().EnumAudioEndpoints(ERender, DeviceStateActive, out devices), "Enumerate active render endpoints");
        uint count;
        Check(devices.GetCount(out count), "Read endpoint count");
        for (uint i = 0; i < count; i++)
        {
            IMMDevice device;
            Check(devices.Item(i, out device), "Read render endpoint");
            string id = ReadId(device);
            string name;
            try { name = ReadFriendlyName(device); }
            catch { name = null; }
            result.Add((String.IsNullOrWhiteSpace(name) ? "(unnamed output)" : name) + " | ID: " + id);
        }
        return result.ToArray();
    }

    public static bool IsActiveRenderEndpoint(string selectedId)
    {
        IMMDeviceCollection devices;
        Check(Enumerator().EnumAudioEndpoints(ERender, DeviceStateActive, out devices), "Enumerate active render endpoints");
        uint count;
        Check(devices.GetCount(out count), "Read endpoint count");
        for (uint i = 0; i < count; i++)
        {
            IMMDevice device;
            Check(devices.Item(i, out device), "Read render endpoint");
            if (String.Equals(ReadId(device), selectedId, StringComparison.Ordinal)) return true;
        }
        return false;
    }

    public static void Capture(string endpointId, string outputDirectory)
    {
        if (!IsActiveRenderEndpoint(endpointId)) throw new InvalidOperationException("EndpointId must match an active render endpoint; no default endpoint is selected automatically.");
        if (File.Exists(Path.Combine(outputDirectory, "recording-complete.json")))
            throw new IOException("recording-complete.json already exists; refusing to reuse this output directory.");
        try { CaptureReserved(endpointId, outputDirectory); }
        catch (Exception ex)
        {
            throw new IOException("Capture did not complete successfully. Any already written WAV parts were retained; without recording-complete.json they are incomplete. " + ex.Message, ex);
        }
    }

    static void CaptureReserved(string endpointId, string outputDirectory)
    {
        IMMDevice device;
        Check(Enumerator().GetDevice(endpointId, out device), "Open selected endpoint");
        uint state;
        Check(device.GetState(out state), "Check selected endpoint");
        if ((state & DeviceStateActive) == 0) throw new InvalidOperationException("Selected endpoint is no longer active.");

        Guid audioClientId = IidAudioClient;
        object clientObject;
        Check(device.Activate(ref audioClientId, 23 /* CLSCTX_ALL */, IntPtr.Zero, out clientObject), "Activate audio client");
        var client = (IAudioClient)clientObject;
        IntPtr format = IntPtr.Zero;
        bool started = false;
        int parts = 0;
        long peak = 0;
        long nonZero = 0;
        var pending = new List<short>(80000);
        StreamingResampler resampler = null;
        bool writeFailed = false;
        Action<IList<short>> appendSamples = delegate(IList<short> samples)
        {
            foreach (short sample in samples)
            {
                pending.Add(sample);
                long magnitude = Math.Abs((long)sample);
                if (magnitude > peak) peak = magnitude;
                if (sample != 0) nonZero++;
            }
            while (pending.Count >= 80000)
            {
                try { WritePart(outputDirectory, parts + 1, pending.GetRange(0, 80000)); }
                catch { writeFailed = true; throw; }
                pending.RemoveRange(0, 80000);
                parts++;
            }
        };
        try
        {
            Check(client.GetMixFormat(out format), "Read output mix format");
            var wf = (WaveFormatEx)Marshal.PtrToStructure(format, typeof(WaveFormatEx));
            bool isFloat = wf.FormatTag == 3;
            if (wf.FormatTag == 0xfffe)
            {
                var subtype = (Guid)Marshal.PtrToStructure(IntPtr.Add(format, 24), typeof(Guid));
                isFloat = subtype == FloatSubtype;
                if (subtype != FloatSubtype && subtype != PcmSubtype) throw new NotSupportedException("Unsupported render format subtype: " + subtype);
            }
            else if (wf.FormatTag != 1 && wf.FormatTag != 3) throw new NotSupportedException("Unsupported render format tag: " + wf.FormatTag);
            if (wf.Channels == 0 || wf.SamplesPerSec == 0 || wf.BlockAlign == 0 || (isFloat ? wf.BitsPerSample != 32 : wf.BitsPerSample != 16))
                throw new NotSupportedException("Only 16-bit PCM or 32-bit float output formats are supported by this helper.");
            resampler = new StreamingResampler((int)wf.SamplesPerSec);

            Check(client.Initialize(ShareModeShared, Loopback, 10000000, 0, format, IntPtr.Zero), "Initialize WASAPI loopback");
            object captureObject;
            Guid captureId = IidCaptureClient;
            Check(client.GetService(ref captureId, out captureObject), "Get loopback capture service");
            var capture = (IAudioCaptureClient)captureObject;
            Check(client.Start(), "Start WASAPI loopback");
            started = true;
            int stopRequested = 0;
            ConsoleCancelEventHandler handler = delegate(object sender, ConsoleCancelEventArgs args) { args.Cancel = true; Interlocked.Exchange(ref stopRequested, 1); };
            Console.CancelKeyPress += handler;
            Exception captureError = null;
            try
            {
                Console.WriteLine("Capturing selected render endpoint. Press Ctrl+C to stop normally.");
                int drainedPackets = 0;
                int capturedPackets = 0;
                while (true)
                {
                    bool stopping = Volatile.Read(ref stopRequested) != 0;
                    if (stopping && drainedPackets >= 64) break;
                    uint packetFrames;
                    Check(capture.GetNextPacketSize(out packetFrames), "Poll loopback packet");
                    if (packetFrames == 0) { if (stopping) break; Thread.Sleep(10); continue; }
                    IntPtr data; uint frames, flags; ulong devPos, qpcPos;
                    Check(capture.GetBuffer(out data, out frames, out flags, out devPos, out qpcPos), "Read loopback packet");
                    double[] packetMono;
                    try
                    {
                        packetMono = new double[(int)frames];
                        for (int frame = 0; frame < frames; frame++)
                        {
                            double sum = 0;
                            for (int channel = 0; channel < wf.Channels; channel++)
                            {
                                int offset = frame * wf.BlockAlign + channel * (wf.BitsPerSample / 8);
                                if ((flags & BufferSilent) == 0)
                                {
                                    if (isFloat) sum += Marshal.PtrToStructure<float>(IntPtr.Add(data, offset));
                                    else sum += Marshal.ReadInt16(data, offset) / 32768.0;
                                }
                            }
                            packetMono[frame] = (flags & BufferSilent) != 0 ? 0 : sum / wf.Channels;
                        }
                    }
                    finally { Check(capture.ReleaseBuffer(frames), "Release loopback packet"); }
                    // Resample and any potentially slow file writes happen only after ReleaseBuffer.
                    appendSamples(resampler.Push(packetMono, false));
                    if ((flags & BufferDiscontinuity) != 0 && capturedPackets > 0)
                        throw new IOException("WASAPI reported a data discontinuity; audio may be incomplete.");
                    capturedPackets++;
                    if (stopping) drainedPackets++;
                }
            }
            catch (Exception ex) { captureError = ex; }
            finally
            {
                Console.CancelKeyPress -= handler;
                if (started)
                {
                    try { Check(client.Stop(), "Stop WASAPI loopback"); started = false; }
                    catch (Exception stopError) { if (captureError == null) captureError = stopError; }
                }
            }
            if (captureError != null)
            {
                if (!writeFailed)
                {
                    try
                    {
                        appendSamples(resampler.Push(new double[0], true));
                        if (pending.Count > 0) { WritePart(outputDirectory, ++parts, pending); pending.Clear(); }
                    }
                    catch (Exception saveError) { captureError = new AggregateException(captureError, saveError); }
                }
                throw new IOException("Capture failed; buffered audio was saved where possible, and no completion marker was written. " + captureError.Message, captureError);
            }
            appendSamples(resampler.Push(new double[0], true));
            if (pending.Count > 0)
            {
                try { WritePart(outputDirectory, parts + 1, pending); parts++; pending.Clear(); }
                catch { writeFailed = true; throw; }
            }
            if (parts == 0 || peak == 0 || nonZero == 0)
                throw new InvalidOperationException("No non-silent render audio was observed. WAV parts may exist, but no completion marker was written.");
            WriteNew(Path.Combine(outputDirectory, "recording-complete.json"), System.Text.Encoding.UTF8.GetBytes(CompletionMarker(parts)));
        }
        finally
        {
            if (started) { try { client.Stop(); } catch { } }
            if (format != IntPtr.Zero) Marshal.FreeCoTaskMem(format);
        }
    }

    static short ToPcm16(double value)
    {
        if (Double.IsNaN(value) || Double.IsInfinity(value)) value = 0;
        value = Math.Max(-1.0, Math.Min(1.0, value));
        int scaled = value < 0 ? (int)Math.Round(value * 32768.0) : (int)Math.Round(value * 32767.0);
        return (short)Math.Max(-32768, Math.Min(32767, scaled));
    }

    // Centered windowed-sinc low-pass resampler. Its input history and phase survive
    // packet boundaries; storage is pruned to the FIR support after each Push.
    sealed class StreamingResampler
    {
        const int HalfTaps = 32;
        readonly int sourceRate;
        readonly double cutoff;
        readonly List<double> input = new List<double>(2048);
        long baseFrame, totalFrames, nextOutput;

        public StreamingResampler(int rate)
        {
            if (rate < 8000 || rate > 384000) throw new NotSupportedException("Unsupported endpoint sample rate: " + rate);
            sourceRate = rate;
            cutoff = 0.45 * Math.Min(1.0, 16000.0 / rate);
        }

        public List<short> Push(double[] samples, bool final)
        {
            for (int i = 0; i < samples.Length; i++) input.Add(samples[i]);
            totalFrames += samples.Length;
            long outputLimit = final ? totalFrames * 16000L / sourceRate : Int64.MaxValue;
            var output = new List<short>();
            while (nextOutput < outputLimit)
            {
                double center = nextOutput * (double)sourceRate / 16000.0;
                if (!final && center + HalfTaps >= totalFrames) break;
                long centerFrame = (long)Math.Floor(center);
                double sum = 0, normalization = 0;
                for (int tap = -HalfTaps; tap <= HalfTaps; tap++)
                {
                    long sourceFrame = centerFrame + tap;
                    double distance = sourceFrame - center;
                    double absDistance = Math.Abs(distance);
                    if (absDistance > HalfTaps) continue;
                    double window = 0.54 + 0.46 * Math.Cos(Math.PI * distance / (HalfTaps + 1));
                    double x = 2.0 * cutoff * distance;
                    double sinc = Math.Abs(x) < 1e-12 ? 1.0 : Math.Sin(Math.PI * x) / (Math.PI * x);
                    double weight = 2.0 * cutoff * sinc * window;
                    normalization += weight;
                    if (sourceFrame >= 0 && sourceFrame < totalFrames && sourceFrame >= baseFrame)
                        sum += input[(int)(sourceFrame - baseFrame)] * weight;
                }
                output.Add(ToPcm16(normalization == 0 ? 0 : sum / normalization));
                nextOutput++;
            }
            long firstNeeded = (long)Math.Floor(nextOutput * (double)sourceRate / 16000.0) - HalfTaps;
            int discard = (int)Math.Max(0, Math.Min(input.Count, firstNeeded - baseFrame));
            if (discard > 0) { input.RemoveRange(0, discard); baseFrame += discard; }
            return output;
        }
    }

    static byte[] EncodeWav(short[] samples)
    {
        if (samples == null || samples.Length == 0) throw new ArgumentException("WAV requires at least one PCM sample.");
        byte[] wav = new byte[44 + samples.Length * 2];
        Buffer.BlockCopy(System.Text.Encoding.ASCII.GetBytes("RIFF"), 0, wav, 0, 4);
        Put32(wav, 4, (uint)(wav.Length - 8));
        Buffer.BlockCopy(System.Text.Encoding.ASCII.GetBytes("WAVEfmt "), 0, wav, 8, 8);
        Put32(wav, 16, 16); Put16(wav, 20, 1); Put16(wav, 22, 1);
        Put32(wav, 24, 16000); Put32(wav, 28, 32000); Put16(wav, 32, 2); Put16(wav, 34, 16);
        Buffer.BlockCopy(System.Text.Encoding.ASCII.GetBytes("data"), 0, wav, 36, 4);
        Put32(wav, 40, (uint)(samples.Length * 2));
        for (int i = 0; i < samples.Length; i++) Put16(wav, 44 + i * 2, unchecked((ushort)samples[i]));
        return wav;
    }
    static void Put16(byte[] target, int at, ushort value) { target[at] = (byte)value; target[at + 1] = (byte)(value >> 8); }
    static void Put32(byte[] target, int at, uint value) { Put16(target, at, (ushort)value); Put16(target, at + 2, (ushort)(value >> 16)); }

    static void WritePart(string directory, int sequence, List<short> samples)
    {
        WritePart(directory, sequence, samples.ToArray());
    }
    static void WritePart(string directory, int sequence, short[] samples)
    {
        string path = Path.Combine(directory, "part-" + sequence.ToString("D6", System.Globalization.CultureInfo.InvariantCulture) + ".wav");
        WriteNew(path, EncodeWav(samples));
    }
    static void WriteNew(string path, byte[] data)
    {
        string temporary = Path.Combine(Path.GetDirectoryName(path), "." + Path.GetFileName(path) + "." + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                stream.Write(data, 0, data.Length);
                stream.Flush(true);
            }
            // Same-directory move is atomic on Windows; it fails rather than replacing an existing target.
            File.Move(temporary, path);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    static string CompletionMarker(int parts)
    {
        if (parts < 1) throw new ArgumentOutOfRangeException("parts");
        return "{\"version\":1,\"parts\":" + parts.ToString(System.Globalization.CultureInfo.InvariantCulture) + "}";
    }

    public static void SelfTest()
    {
        if (typeof(IMMDeviceCollection).GUID != new Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"))
            throw new Exception("IMMDeviceCollection interface identifier mismatch.");
        short[] converted = { ToPcm16(-1), ToPcm16(-0.5), ToPcm16(0), ToPcm16(0.5), ToPcm16(1) };
        if (converted[0] != -32768 || converted[1] != -16384 || converted[2] != 0 || converted[3] != 16384 || converted[4] != 32767)
            throw new Exception("Float-to-PCM16 conversion contract failed.");
        byte[] wav = EncodeWav(new short[] { -32768, 0, 32767 });
        if (wav.Length != 50 || System.Text.Encoding.ASCII.GetString(wav, 0, 4) != "RIFF" ||
            System.Text.Encoding.ASCII.GetString(wav, 8, 8) != "WAVEfmt " ||
            BitConverter.ToUInt32(wav, 24) != 16000 || BitConverter.ToUInt16(wav, 22) != 1 ||
            BitConverter.ToUInt16(wav, 34) != 16 || BitConverter.ToUInt32(wav, 40) != 6 ||
            BitConverter.ToInt16(wav, 44) != -32768 || BitConverter.ToInt16(wav, 48) != 32767)
            throw new Exception("16 kHz mono PCM WAV contract failed.");
        if (CompletionMarker(2) != "{\"version\":1,\"parts\":2}") throw new Exception("Completion marker contract failed.");

        const int rate = 48000;
        var signal = new double[12000];
        for (int i = 0; i < signal.Length; i++)
            signal[i] = 0.3 * Math.Sin(2 * Math.PI * 997 * i / rate) + 0.1 * Math.Sin(2 * Math.PI * 12000 * i / rate);
        var whole = new StreamingResampler(rate).Push(signal, true);
        var splitResampler = new StreamingResampler(rate);
        var split = new List<short>();
        for (int offset = 0; offset < signal.Length; offset += 137)
        {
            int length = Math.Min(137, signal.Length - offset);
            var packet = new double[length]; Array.Copy(signal, offset, packet, 0, length);
            split.AddRange(splitResampler.Push(packet, false));
        }
        split.AddRange(splitResampler.Push(new double[0], true));
        if (whole.Count != split.Count) throw new Exception("Packet-split resampler output length changed.");
        for (int i = 0; i < whole.Count; i++) if (whole[i] != split[i]) throw new Exception("Packet-split resampler output changed at sample " + i + ".");

        var high = new double[12000];
        for (int i = 0; i < high.Length; i++) high[i] = 0.5 * Math.Sin(2 * Math.PI * 12000 * i / rate);
        var rejected = new StreamingResampler(rate).Push(high, true);
        double energy = 0; int measured = 0;
        for (int i = 100; i < rejected.Count - 100; i++) { energy += rejected[i] * (double)rejected[i]; measured++; }
        double rms = Math.Sqrt(energy / measured);
        if (rms > 200) throw new Exception("Out-of-band 12 kHz tone was not adequately rejected: RMS=" + rms);
    }
}
